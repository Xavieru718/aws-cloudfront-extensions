import copy
import http
import json
import os
import time
from datetime import datetime
from typing import Any, List

import boto3
from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import APIGatewayRestResolver

from functions.ssl_api_handler.types_ import Input
from layer.acm_service.client import AcmUtilsService
from layer.acm_service.types_ import Certificate, ImportCertificateInput, Tag
from layer.common.cert_utils import get_domain_list_from_cert
from layer.common.domain_utils import validate_input_parameters
from layer.common.file_utils import convert_string_to_file
from layer.common.response import Response
from layer.common.types_ import Cname, SourceCfInfo
from layer.job_service.client import JobInfoUtilsService
from layer.job_service.types_ import JobInfo
from layer.resouce_service.client import ResourceUtilService
from layer.sns_service.client import SnsUtilsService
from layer.stepfunction_service.client import StepFunctionUtilsService

raw_context = {
    'aws_request_id': ''
}

app = APIGatewayRestResolver()
# tracer = Tracer(service="ssl_for_saas_appsync_resolver")
logger = Logger(service="ssl-api")
job_info_client = JobInfoUtilsService(logger=logger)
acm_client = AcmUtilsService(logger=logger)
sns_client = SnsUtilsService(logger=logger)
# get sns topic arn from environment variable
sns_topic_arn = os.environ.get('SNS_TOPIC')
resource_client = ResourceUtilService(logger=logger)
stepfunctions_client = StepFunctionUtilsService(logger=logger)
step_function_arn = os.environ.get('STEP_FUNCTION_ARN')

FILE_FOLDER = '/tmp'
PEM_FILE = FILE_FOLDER + "/cert.pem"
_GET_FILE = lambda x: open(os.path.join(FILE_FOLDER, x), "rb").read()
lambda_client = boto3.client('lambda')
STATUS_UPDATE_LAMBDA_FUNCTION = os.environ.get('STATUS_UPDATE_LAMBDA_FUNCTION')


# fixme: same as app_sync
@app.post("/ssl_for_saas")
def cert_create_or_import():
    global raw_context
    job_id = raw_context.aws_request_id
    # will use request id as job_id
    logger.info(job_id)
    sns_msg = []
    try:

        # Get the parameters from the event
        body: dict = app.current_event.json_body
        logger.info(f'body: {app.current_event}, type: {type(app.current_event.json_body)}')
        event = Input(**body)
        # validate the input
        validate_input_parameters(body)
        acm_op = body['acm_op']
        auto_creation = body['auto_creation']
        domain_name_list = body['cnameList']
        # fixme: not sure this is the right checking
        # fixme: as we have already used the cnameList above
        if 'cnameList' in body:
            certTotalNumber = len(body['cnameList'])
        else:
            certTotalNumber = 0
            body['cnameList'] = []

        if 'pemList' in body:
            pemTotalNumber = len(body['pemList'])
        else:
            pemTotalNumber = 0
            body['pemList'] = []

        cloudfront_total_number = 0 if (auto_creation == 'false') else certTotalNumber
        job_type = body['acm_op']
        creation_date = str(datetime.now())
        cert_create_stage_status = 'INPROGRESS'
        cert_validation_stage_status = 'NOTSTART'
        dist_stage_status = 'NONEED' if (auto_creation == 'false') else 'NOTSTART'

        # remove the pemList from input body since PEM content is too large and not suitable for save in DDB
        body_without_pem = copy.deepcopy(body)
        del body_without_pem['pemList']
        if auto_creation == "false":
            job_info_client.create_job_info(JobInfo(
                jobId=job_id,
                job_input=json.dumps(body_without_pem, indent=4, default=str),
                cert_total_number=certTotalNumber if acm_op == "create" else pemTotalNumber,
                cloudfront_distribution_total_number=cloudfront_total_number,
                cert_completed_number=0,
                cloudfront_distribution_created_number=0,
                jobType=job_type,
                creationDate=creation_date,
                certCreateStageStatus=cert_create_stage_status,
                certValidationStageStatus=cert_validation_stage_status,
                distStageStatus=dist_stage_status,
                promptInfo='',
                certList=[],
                dcv_validation_msg='',
                distList=[]
            ))
            if acm_op == 'create':
                # aggregate certificate if dist_aggregate is true
                # if dist_aggregate == "true":
                #     aggregate_cert_operation(certTotalNumber, domain_name_list, raw_context)
                # # otherwise, create certificate for each cname
                # else:
                for cname_index, cname_value in enumerate(domain_name_list):
                    cert_arn = acm_client.request_certificate(Certificate(
                        DomainName=cname_value['domainName'],
                        SubjectAlternativeNames=cname_value['sanList'],
                        CertUUID='',
                        TaskToken='',
                        JobToken='',
                        TaskType='',
                    ))
                    logger.info('Certificate creation response: %s', cert_arn)

                    acm_client.tag_job_certificate(cert_arn, job_id)
                    cert_detail = acm_client.fetch_dcv_value(cert_arn)

                    # iterate DomainValidationOptions array to get DNS validation record
                    for dns_index, dns_value in enumerate(cert_detail['DomainValidationOptions']):
                        if dns_value['ValidationMethod'] == 'DNS':
                            dns_validation_record = dns_value['ResourceRecord']
                            logger.info('index %s: DNS validation record: %s', dns_index, dns_validation_record)
                            sns_msg.append(dns_validation_record)

                job_info_client.update_job_fields_by_dict(job_id, {
                    'certCreateStageStatus': 'SUCCESS',
                    'certValidationStageStatus': 'INPROGRESS',
                    'cert_completed_number': certTotalNumber,
                    'dcv_validation_msg': sns_client.generate_notify_content(str(sns_msg))
                })
                sns_client.notify_sns_subscriber(str(sns_msg), sns_topic_arn)
            elif acm_op == 'import':
                # iterate pemList array from event
                for pem_index, pem_value in enumerate(event['pemList']):
                    convert_string_to_file(pem_value['CertPem'], PEM_FILE)
                    _domainList = get_domain_list_from_cert(PEM_FILE, logger)
                    domain_name = (_domainList[0] if _domainList else '').replace('*.', '')

                    certificate = ImportCertificateInput(
                        Certificate=str.encode(pem_value['CertPem']),
                        CertificateChain=str.encode(pem_value['ChainPem']),
                        PrivateKey=str.encode(pem_value['PrivateKeyPem']),
                        CertificateArn='',
                        Tags=[Tag(
                            Key='issuer',
                            Value=domain_name,
                        )]
                    )
                    cert_arn = acm_client.import_certificate_by_pem(certificate)
                    acm_client.tag_job_certificate(cert_arn, job_id)

                job_info_client.update_job_fields_by_dict(job_id, {
                    'certCreateStageStatus': 'SUCCESS',
                    'certValidationStageStatus': 'SUCCESS',
                    'cert_completed_number': pemTotalNumber,
                })

            return Response(statusCode=http.HTTPStatus.OK, body=job_id)

        # invoke step function to implement streamlined process of cert create/import and distribution create
        elif auto_creation == "true":
            # invoke existing step function
            logger.info('auto_creation is true, invoke step function with body %s', str(body))
            body['aws_request_id'] = raw_context.aws_request_id
            if acm_op == "import":
                # iterate pemList array from event
                gen_cname_info_list = []
                for pem_index, pem_value in enumerate(body['pemList']):
                    convert_string_to_file(pem_value['CertPem'], PEM_FILE)
                    _domainList = get_domain_list_from_cert(PEM_FILE, logger)
                    _domainName = _domainList[0] if _domainList else ''
                    if 'config_version_id' in pem_value['existing_cf_info']:
                        tmpCnameInfo = Cname(
                            domainName=_domainName,
                            sanList=_domainList,
                            existing_cf_info=SourceCfInfo(
                                distribution_id=pem_value['existing_cf_info']['distribution_id'],
                                config_version_id=pem_value['existing_cf_info']['config_version_id']
                            ),
                            originsItemsDomainName=''
                        )
                    else:
                        tmpCnameInfo = Cname(
                            domainName=_domainName,
                            sanList=_domainList,
                            existing_cf_info=SourceCfInfo(
                                distribution_id=pem_value['existing_cf_info']['distribution_id'],
                            ),
                            originsItemsDomainName=''
                        )
                    gen_cname_info_list.append(tmpCnameInfo)

                body['cnameList'] = gen_cname_info_list

            job_info_client.create_job_info(JobInfo(
                jobId=raw_context.aws_request_id,
                job_input=json.dumps(body_without_pem, indent=4, default=str),
                cert_total_number=certTotalNumber if acm_op == "create" else pemTotalNumber,
                cloudfront_distribution_total_number=cloudfront_total_number,
                cert_completed_number=0,
                cloudfront_distribution_created_number=0,
                jobType=job_type,
                creationDate=creation_date,
                certCreateStageStatus=cert_create_stage_status,
                certValidationStageStatus=cert_validation_stage_status,
                distStageStatus=dist_stage_status,
                promptInfo='',
                certList=[],
                dcv_validation_msg='',
                distList=[]
            ))

            stepfunctions_client.invoke_step_function(step_function_arn, body)
            return Response(statusCode=http.HTTPStatus.OK, body=raw_context.aws_request_id)
        else:
            logger.info('auto_creation is not true or false')
            return Response(statusCode=http.HTTPStatus.INTERNAL_SERVER_ERROR, body='error: auto_creation is not true or false')
    except Exception as e:
        logger.error("Exception occurred, just update the ddb table")
        job_info_client.update_job_fields_by_dict(job_id, {
            'certCreateStageStatus': 'FAILED',
            'promptInfo': str(e)
        })
        raise e


@app.get("/ssl_for_saas/cert_list")
def manager_certification_list():
    certificates = acm_client.list_certificates()
    return Response(statusCode=http.HTTPStatus.OK, body=json.dumps(certificates, indent=4, sort_keys=True, default=str))


@app.get("/ssl_for_saas/list_ssl_certification_with_jobId")
def manager_certification_list_jobId():
    job_id = app.current_event.get_query_string_value(name="jobId", default_value="")
    certificates = acm_client.list_certificates()
    return acm_client.get_results(job_id=job_id, certificates=certificates)


@app.get("/ssl_for_saas/list_cloudfront_arn_with_jobId")
def manager_cloudfront_arn_list_with_jobId(job_id) -> List[str]:
    return resource_client.get_resource_by_job_id(job_id)


@app.get("/ssl_for_saas/list_ssl_jobs")
def manager_list_ssl_jobs() -> List[dict[str, Any]]:
    return job_info_client.get_all()


@app.get("/ssl_for_saas/get_ssl_job")
def manager_get_ssl_job(job_id):
    lambda_payload = {
        'job_id': job_id
    }
    try:
        lambda_client.invoke(
            FunctionName=STATUS_UPDATE_LAMBDA_FUNCTION,
            InvocationType='Event',
            Payload=json.dumps(lambda_payload).encode('UTF-8')
        )
    except Exception as e:
        logger.error("Failed to call lambda function with error" + str(e))

    try:
        job_info = job_info_client.get_job_info_by_id(job_id)
        logger.info(job_info)
        if not job_id:
            time.sleep(3)
            job_info = job_info_client.get_job_info_by_id(job_id)
            return job_info
        return job_info
    except Exception as e:
        return Response(statusCode=http.HTTPStatus.INTERNAL_SERVER_ERROR, body="Can not found job info with id:" + job_id)


def handler(event: Any, context: Any):
    global raw_event
    raw_event = event
    global raw_context
    raw_context = context
    return app.resolve(event, context)
