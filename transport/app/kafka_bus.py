"""Kafka producer/consumer для сегментов ответа."""

import asyncio
import logging

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import TopicAlreadyExistsError

from .config import Settings


logger = logging.getLogger("content_ideas_transport.kafka")


class KafkaBus:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.producer: AIOKafkaProducer | None = None
        self.consumer: AIOKafkaConsumer | None = None
        self.supervisor: asyncio.Task | None = None
        self.ready = False

    async def start(self, on_message) -> None:
        self.supervisor = asyncio.create_task(self._supervise(on_message))

    async def stop(self) -> None:
        if self.supervisor:
            self.supervisor.cancel()
            await asyncio.gather(self.supervisor, return_exceptions=True)
            self.supervisor = None
        await self._disconnect()

    async def _ensure_topic(self) -> None:
        admin = AIOKafkaAdminClient(bootstrap_servers=self.settings.kafka_bootstrap_servers)
        await admin.start()
        try:
            topics = await admin.list_topics()
            if self.settings.kafka_topic not in topics:
                try:
                    await admin.create_topics(
                        [NewTopic(name=self.settings.kafka_topic, num_partitions=1, replication_factor=1)]
                    )
                except TopicAlreadyExistsError:
                    pass
        finally:
            await admin.close()

    async def _connect(self) -> None:
        await self._ensure_topic()
        self.producer = AIOKafkaProducer(bootstrap_servers=self.settings.kafka_bootstrap_servers)
        await self.producer.start()
        self.consumer = AIOKafkaConsumer(
            self.settings.kafka_topic,
            bootstrap_servers=self.settings.kafka_bootstrap_servers,
            group_id=self.settings.kafka_group_id,
            auto_offset_reset="latest",
            enable_auto_commit=False,
        )
        await self.consumer.start()
        self.ready = True
        logger.info("Kafka подключена, топик %s", self.settings.kafka_topic)

    async def _disconnect(self) -> None:
        self.ready = False
        consumer, producer = self.consumer, self.producer
        self.consumer = None
        self.producer = None
        if consumer:
            try:
                await consumer.stop()
            except Exception:
                logger.exception("Ошибка остановки Kafka consumer")
        if producer:
            try:
                await producer.stop()
            except Exception:
                logger.exception("Ошибка остановки Kafka producer")

    async def _supervise(self, on_message) -> None:
        while True:
            try:
                await self._connect()
                assert self.consumer is not None
                async for record in self.consumer:
                    try:
                        await on_message(record.value)
                    except Exception:
                        logger.exception("Не удалось обработать запись Kafka")
                        # Повторная обработка записи не исправит внутреннюю ошибку приложения.
                    await self.consumer.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Kafka недоступна; следующая попытка через 5 секунд")
            finally:
                await self._disconnect()
            await asyncio.sleep(5)

    async def publish(self, message_id: str, payload: bytes) -> None:
        if not self.ready or self.producer is None:
            raise RuntimeError("Kafka недоступна")
        await asyncio.wait_for(
            self.producer.send_and_wait(
                self.settings.kafka_topic,
                key=message_id.encode("ascii"),
                value=payload,
            ),
            timeout=10,
        )

    async def check_ready(self) -> bool:
        if not self.ready:
            return False
        try:
            admin = AIOKafkaAdminClient(bootstrap_servers=self.settings.kafka_bootstrap_servers)
            await asyncio.wait_for(admin.start(), timeout=3)
            try:
                topics = await asyncio.wait_for(admin.list_topics(), timeout=3)
                return self.settings.kafka_topic in topics
            finally:
                await admin.close()
        except Exception:
            return False
