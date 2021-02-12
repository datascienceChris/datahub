import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List

import confluent_kafka
from confluent_kafka.schema_registry.schema_registry_client import SchemaRegistryClient

import gometa.ingestion.extractor.schema_util as schema_util
from gometa.configuration import ConfigModel
from gometa.configuration.common import AllowDenyPattern
from gometa.configuration.kafka import KafkaConsumerConnectionConfig
from gometa.ingestion.api.common import PipelineContext
from gometa.ingestion.api.source import Source, SourceReport
from gometa.ingestion.source.metadata_common import MetadataWorkUnit
from gometa.metadata.com.linkedin.pegasus2avro.common import AuditStamp, Status
from gometa.metadata.com.linkedin.pegasus2avro.metadata.snapshot import DatasetSnapshot
from gometa.metadata.com.linkedin.pegasus2avro.mxe import MetadataChangeEvent
from gometa.metadata.com.linkedin.pegasus2avro.schema import (
    KafkaSchema,
    SchemaField,
    SchemaMetadata,
)

logger = logging.getLogger(__name__)


class KafkaSourceConfig(ConfigModel):
    connection: KafkaConsumerConnectionConfig = KafkaConsumerConnectionConfig()
    topic_patterns: AllowDenyPattern = AllowDenyPattern(allow=[".*"], deny=["^_.*"])


@dataclass
class KafkaSourceReport(SourceReport):
    topics_scanned = 0
    warnings: Dict[str, List[str]] = field(default_factory=dict)
    failures: Dict[str, List[str]] = field(default_factory=dict)
    filtered: List[str] = field(default_factory=list)

    def report_topic_scanned(self, topic: str) -> None:
        self.topics_scanned += 1

    def report_warning(self, topic: str, reason: str) -> None:
        if topic not in self.warnings:
            self.warnings[topic] = []
        self.warnings[topic].append(reason)

    def report_failure(self, topic: str, reason: str) -> None:
        if topic not in self.failures:
            self.failures[topic] = []
        self.failures[topic].append(reason)

    def report_dropped(self, topic: str) -> None:
        self.filtered.append(topic)


@dataclass
class KafkaSource(Source):
    source_config: KafkaSourceConfig
    consumer: confluent_kafka.Consumer
    report: KafkaSourceReport

    def __init__(self, config: KafkaSourceConfig, ctx: PipelineContext):
        super().__init__(ctx)
        self.source_config = config
        self.consumer = confluent_kafka.Consumer(
            {
                "group.id": "test",
                "bootstrap.servers": self.source_config.connection.bootstrap,
                **self.source_config.connection.consumer_config,
            }
        )
        self.schema_registry_client = SchemaRegistryClient(
            {"url": self.source_config.connection.schema_registry_url}
        )
        self.report = KafkaSourceReport()

    @classmethod
    def create(cls, config_dict, ctx):
        config = KafkaSourceConfig.parse_obj(config_dict)
        return cls(config, ctx)

    def get_workunits(self) -> Iterable[MetadataWorkUnit]:
        topics = self.consumer.list_topics().topics
        for t in topics:
            self.report.report_topic_scanned(t)

            if self.source_config.topic_patterns.allowed(t):
                mce = self._extract_record(t)
                wu = MetadataWorkUnit(id=f"kafka-{t}", mce=mce)
                self.report.report_workunit(wu)
                yield wu
            else:
                self.report.report_dropped(t)

    def _extract_record(self, topic: str) -> MetadataChangeEvent:
        logger.debug(f"topic = {topic}")
        platform = "kafka"
        dataset_name = topic
        env = "PROD"  # TODO: configure!
        actor, sys_time = "urn:li:corpuser:etl", int(time.time()) * 1000

        metadata_record = MetadataChangeEvent()
        dataset_snapshot = DatasetSnapshot(
            urn=f"urn:li:dataset:(urn:li:dataPlatform:{platform},{dataset_name},{env})",
        )
        dataset_snapshot.aspects.append(Status(removed=False))
        metadata_record.proposedSnapshot = dataset_snapshot

        # Fetch schema from the registry.
        has_schema = True
        try:
            registered_schema = self.schema_registry_client.get_latest_version(
                topic + "-value"
            )
            schema = registered_schema.schema
        except Exception as e:
            self.report.report_warning(topic, f"failed to get schema: {e}")
            has_schema = False

        # Parse the schema
        fields: List[SchemaField] = []
        if has_schema and schema.schema_type == "AVRO":
            fields = schema_util.avro_schema_to_mce_fields(schema.schema_str)
        elif has_schema:
            self.report.report_warning(
                topic, f"unable to parse kafka schema type {schema.schema_type}"
            )

        if has_schema:
            schema_metadata = SchemaMetadata(
                schemaName=topic,
                version=0,
                hash=str(schema._hash),
                platform=f"urn:li:dataPlatform:{platform}",
                platformSchema=KafkaSchema(documentSchema=schema.schema_str),
                fields=fields,
                created=AuditStamp(time=sys_time, actor=actor),
                lastModified=AuditStamp(time=sys_time, actor=actor),
            )
            dataset_snapshot.aspects.append(schema_metadata)

        return metadata_record

    def get_report(self):
        return self.report

    def close(self):
        if self.consumer:
            self.consumer.close()
