import pathlib
import unittest.mock
from datetime import datetime, timedelta, timezone

import jsonpickle

from datahub.ingestion.api.common import PipelineContext
from datahub.ingestion.run.pipeline import Pipeline
from datahub.ingestion.source.bigquery_usage import (
    BigQueryUsageConfig,
    BigQueryUsageSource,
)
from tests.test_helpers import mce_helpers

WRITE_REFERENCE_FILE = False


def test_config_time_defaults():
    config = BigQueryUsageConfig.parse_obj(
        dict(
            project_id="sample-bigquery-project-name-1234",
            bucket_duration="HOUR",
        )
    )
    assert (config.end_time - config.start_time) == timedelta(hours=1)


def test_bq_usage_source(pytestconfig, tmp_path):
    # from google.cloud.logging_v2 import ProtobufEntry

    test_resources_dir: pathlib.Path = (
        pytestconfig.rootpath / "tests/integration/bigquery-usage"
    )
    bigquery_reference_logs_path = test_resources_dir / "bigquery_logs.json"

    if WRITE_REFERENCE_FILE:
        source = BigQueryUsageSource.create(
            dict(
                project_id="harshal-playground-306419",
                start_time=datetime.now(tz=timezone.utc) - timedelta(days=25),
            ),
            PipelineContext(run_id="bq-usage-test"),
        )
        entries = list(source._get_bigquery_log_entries())

        entries = [entry._replace(logger=None) for entry in entries]
        log_entries = jsonpickle.encode(entries, indent=4)
        with bigquery_reference_logs_path.open("w") as logs:
            logs.write(log_entries)

    with unittest.mock.patch(
        "datahub.ingestion.source.bigquery_usage.GCPLoggingClient", autospec=True
    ) as MockClient:
        # Add mock BigQuery API responses.
        with bigquery_reference_logs_path.open() as logs:
            reference_logs = jsonpickle.decode(logs.read())
        MockClient().list_entries.return_value = reference_logs

        # Run a BigQuery usage ingestion run.
        pipeline = Pipeline.create(
            {
                "run_id": "test-bigquery-usage",
                "source": {
                    "type": "bigquery-usage",
                    "config": {"project_id": "sample-bigquery-project-1234"},
                },
                "sink": {
                    "type": "file",
                    "config": {
                        "filename": f"{tmp_path}/bigquery_usages.json",
                    },
                },
            }
        )
        pipeline.run()
        pipeline.raise_from_status()

    output = mce_helpers.load_json_file(str(tmp_path / "bigquery_usages.json"))
    golden = mce_helpers.load_json_file(
        str(test_resources_dir / "bigquery_usages_golden.json")
    )
    mce_helpers.assert_mces_equal(output, golden)
