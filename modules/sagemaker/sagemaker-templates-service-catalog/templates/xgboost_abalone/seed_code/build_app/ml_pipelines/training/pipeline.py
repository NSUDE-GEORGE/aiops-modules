# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Example workflow pipeline script for abalone pipeline.

                                               . -RegisterModel
                                              .
    Process-> Train -> Evaluate -> Condition .
                                              .
                                               . -(stop)

Implements a get_pipeline(**kwargs) method.
"""

import json
import logging
import os
from typing import Any, Optional

import boto3
import sagemaker
import sagemaker.session
from sagemaker.estimator import Estimator
from sagemaker.inputs import TrainingInput
from sagemaker.model_metrics import MetricsSource, ModelMetrics
from sagemaker.network import NetworkConfig
from sagemaker.processing import ProcessingInput, ProcessingOutput, ScriptProcessor
from sagemaker.workflow.condition_step import ConditionStep
from sagemaker.workflow.conditions import ConditionLessThanOrEqualTo
from sagemaker.workflow.functions import JsonGet
from sagemaker.workflow.parameters import ParameterInteger, ParameterString
from sagemaker.workflow.pipeline import Pipeline
from sagemaker.workflow.properties import PropertyFile
from sagemaker.workflow.step_collections import RegisterModel
from sagemaker.workflow.steps import ProcessingStep, TrainingStep

# BASE_DIR = os.path.dirname(os.path.realpath(__file__))

ENABLE_NETWORK_ISOLATION = os.getenv("ENABLE_NETWORK_ISOLATION", "true").lower() == "true"
ENCRYPT_INTER_CONTAINER_TRAFFIC = os.getenv("ENCRYPT_INTER_CONTAINER_TRAFFIC", "true").lower() == "true"
SUBNET_IDS = json.loads(os.getenv("SUBNET_IDS", "[]"))
SECURITY_GROUP_IDS = json.loads(os.getenv("SECURITY_GROUP_IDS", "[]"))


logger = logging.getLogger(__name__)


def get_session(region: str, default_bucket: Optional[str]) -> sagemaker.session.Session:
    """Gets the sagemaker session based on the region.

    Args:
        region: the aws region to start the session
        default_bucket: the bucket to use for storing the artifacts

    Returns:
        `sagemaker.session.Session instance
    """

    boto_session = boto3.Session(region_name=region)

    sagemaker_client = boto_session.client("sagemaker")
    runtime_client = boto_session.client("sagemaker-runtime")
    session = sagemaker.session.Session(
        boto_session=boto_session,
        sagemaker_client=sagemaker_client,
        sagemaker_runtime_client=runtime_client,
        default_bucket=default_bucket,
    )

    return session


def get_pipeline(
    region: str,
    role: Optional[str] = None,
    default_bucket: Optional[str] = None,
    bucket_kms_id: Optional[str] = None,
    model_package_group_name: str = "AbalonePackageGroup",
    pipeline_name: str = "AbalonePipeline",
    base_job_prefix: str = "Abalone",
    project_id: str = "SageMakerProjectId",
) -> Any:
    """Gets a SageMaker ML Pipeline instance working with on abalone data.

    Args:
        region: AWS region to create and run the pipeline.
        role: IAM role to create and run steps and pipeline.
        default_bucket: the bucket to use for storing the artifacts

    Returns:
        an instance of a pipeline
    """

    sagemaker_session = get_session(region, default_bucket)
    if role is None:
        role = sagemaker.session.get_execution_role(sagemaker_session)

    # define network config
    network_config = NetworkConfig(
        subnets=SUBNET_IDS if SUBNET_IDS else None,
        security_group_ids=SECURITY_GROUP_IDS if SECURITY_GROUP_IDS else None,
        enable_network_isolation=ENABLE_NETWORK_ISOLATION,
        encrypt_inter_container_traffic=ENCRYPT_INTER_CONTAINER_TRAFFIC,
    )
    # define network config without network isolation to allow S3 access for preprocessor
    network_config_without_isolation = NetworkConfig(
        subnets=SUBNET_IDS if SUBNET_IDS else None,
        security_group_ids=SECURITY_GROUP_IDS if SECURITY_GROUP_IDS else None,
        enable_network_isolation=False,
        encrypt_inter_container_traffic=ENCRYPT_INTER_CONTAINER_TRAFFIC,
    )

    # parameters for pipeline execution
    processing_instance_count = ParameterInteger(name="ProcessingInstanceCount", default_value=1)
    processing_instance_type = ParameterString(name="ProcessingInstanceType", default_value="ml.m5.xlarge")
    training_instance_type = ParameterString(name="TrainingInstanceType", default_value="ml.m5.xlarge")
    inference_instance_type = ParameterString(name="InferenceInstanceType", default_value="ml.m5.xlarge")  # noqa: F841
    model_approval_status = ParameterString(name="ModelApprovalStatus", default_value="PendingManualApproval")
    input_data = ParameterString(
        name="InputDataUrl",
        default_value=f"s3://sagemaker-servicecatalog-seedcode-{region}/dataset/abalone-dataset.csv",
    )
    processing_image_name = "sagemaker-{0}-processingimagebuild".format(project_id)
    training_image_name = "sagemaker-{0}-trainingimagebuild".format(project_id)
    inference_image_name = "sagemaker-{0}-inferenceimagebuild".format(project_id)

    # processing step for feature engineering
    try:
        processing_image_uri = sagemaker_session.sagemaker_client.describe_image_version(
            ImageName=processing_image_name
        )["ContainerImage"]
    except sagemaker_session.sagemaker_client.exceptions.ResourceNotFound:
        processing_image_uri = sagemaker.image_uris.retrieve(
            framework="xgboost",
            region=region,
            version="1.0-1",
            py_version="py3",
            instance_type="ml.m5.xlarge",
        )
    script_processor = ScriptProcessor(
        image_uri=processing_image_uri,
        instance_type=processing_instance_type,
        instance_count=processing_instance_count,
        base_job_name=f"{base_job_prefix}/sklearn-abalone-preprocess",
        command=["python3"],
        sagemaker_session=sagemaker_session,
        role=role,
        output_kms_key=bucket_kms_id,
        network_config=network_config_without_isolation,
    )
    step_process = ProcessingStep(
        name="PreprocessAbaloneData",
        processor=script_processor,
        outputs=[
            ProcessingOutput(output_name="train", source="/opt/ml/processing/train"),
            ProcessingOutput(output_name="validation", source="/opt/ml/processing/validation"),
            ProcessingOutput(output_name="test", source="/opt/ml/processing/test"),
        ],
        code="source_scripts/preprocessing.py",
        job_arguments=["--input-data", input_data],
    )

    # training step for generating model artifacts
    model_path = f"s3://{default_bucket}/{base_job_prefix}/AbaloneTrain"

    try:
        training_image_uri = sagemaker_session.sagemaker_client.describe_image_version(ImageName=training_image_name)[
            "ContainerImage"
        ]
    except sagemaker_session.sagemaker_client.exceptions.ResourceNotFound:
        training_image_uri = sagemaker.image_uris.retrieve(
            framework="xgboost",
            region=region,
            version="1.0-1",
            py_version="py3",
            instance_type="ml.m5.xlarge",
        )

    xgb_train = Estimator(
        image_uri=training_image_uri,
        instance_type=training_instance_type,
        instance_count=1,
        output_path=model_path,
        base_job_name=f"{base_job_prefix}/abalone-train",
        sagemaker_session=sagemaker_session,
        role=role,
        output_kms_key=bucket_kms_id,
        subnets=SUBNET_IDS if SUBNET_IDS else None,
        security_group_ids=SECURITY_GROUP_IDS if SECURITY_GROUP_IDS else None,
        enable_network_isolation=ENABLE_NETWORK_ISOLATION,
        encrypt_inter_container_traffic=ENCRYPT_INTER_CONTAINER_TRAFFIC,
    )
    xgb_train.set_hyperparameters(
        objective="reg:linear",
        num_round=50,
        max_depth=5,
        eta=0.2,
        gamma=4,
        min_child_weight=6,
        subsample=0.7,
        silent=0,
    )
    step_train = TrainingStep(
        name="TrainAbaloneModel",
        estimator=xgb_train,
        inputs={
            "train": TrainingInput(
                s3_data=step_process.properties.ProcessingOutputConfig.Outputs["train"].S3Output.S3Uri,
                content_type="text/csv",
            ),
            "validation": TrainingInput(
                s3_data=step_process.properties.ProcessingOutputConfig.Outputs["validation"].S3Output.S3Uri,
                content_type="text/csv",
            ),
        },
    )

    # processing step for evaluation
    script_eval = ScriptProcessor(
        image_uri=training_image_uri,
        command=["python3"],
        instance_type=processing_instance_type,
        instance_count=1,
        base_job_name=f"{base_job_prefix}/script-abalone-eval",
        sagemaker_session=sagemaker_session,
        role=role,
        output_kms_key=bucket_kms_id,
        network_config=network_config,
    )
    evaluation_report = PropertyFile(
        name="AbaloneEvaluationReport",
        output_name="evaluation",
        path="evaluation.json",
    )
    step_eval = ProcessingStep(
        name="EvaluateAbaloneModel",
        processor=script_eval,
        inputs=[
            ProcessingInput(
                source=step_train.properties.ModelArtifacts.S3ModelArtifacts,
                destination="/opt/ml/processing/model",
            ),
            ProcessingInput(
                source=step_process.properties.ProcessingOutputConfig.Outputs["test"].S3Output.S3Uri,
                destination="/opt/ml/processing/test",
            ),
        ],
        outputs=[
            ProcessingOutput(output_name="evaluation", source="/opt/ml/processing/evaluation"),
        ],
        code="source_scripts/evaluate.py",
        property_files=[evaluation_report],
    )

    # register model step that will be conditionally executed
    model_metrics = ModelMetrics(
        model_statistics=MetricsSource(
            s3_uri="{}/evaluation.json".format(
                step_eval.arguments["ProcessingOutputConfig"]["Outputs"][0]["S3Output"]["S3Uri"]
            ),
            content_type="application/json",
        )
    )

    try:
        inference_image_uri = sagemaker_session.sagemaker_client.describe_image_version(ImageName=inference_image_name)[
            "ContainerImage"
        ]
    except sagemaker_session.sagemaker_client.exceptions.ResourceNotFound:
        inference_image_uri = sagemaker.image_uris.retrieve(
            framework="xgboost",
            region=region,
            version="1.0-1",
            py_version="py3",
            instance_type="ml.m5.xlarge",
        )
    step_register = RegisterModel(
        name="RegisterAbaloneModel",
        estimator=xgb_train,
        image_uri=inference_image_uri,
        model_data=step_train.properties.ModelArtifacts.S3ModelArtifacts,
        content_types=["text/csv"],
        response_types=["text/csv"],
        inference_instances=["ml.t2.medium", "ml.m5.large"],
        transform_instances=["ml.m5.large"],
        model_package_group_name=model_package_group_name,
        approval_status=model_approval_status,
        model_metrics=model_metrics,
    )

    # condition step for evaluating model quality and branching execution
    cond_lte = ConditionLessThanOrEqualTo(
        left=JsonGet(
            step_name=step_eval.name,
            property_file=evaluation_report,
            json_path="regression_metrics.mse.value",
        ),
        right=6.0,
    )
    step_cond = ConditionStep(
        name="CheckMSEAbaloneEvaluation",
        conditions=[cond_lte],
        if_steps=[step_register],
        else_steps=[],
    )

    # pipeline instance
    pipeline = Pipeline(
        name=pipeline_name,
        parameters=[
            processing_instance_type,
            processing_instance_count,
            training_instance_type,
            model_approval_status,
            input_data,
        ],
        steps=[step_process, step_train, step_eval, step_cond],
        sagemaker_session=sagemaker_session,
    )
    return pipeline
