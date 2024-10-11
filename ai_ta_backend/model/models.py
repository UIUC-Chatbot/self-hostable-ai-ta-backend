"""
Create models for each table in the database.
"""
from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import JSON
from sqlalchemy import Text
from sqlalchemy import VARCHAR
from sqlalchemy import Float
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from uuid import uuid4

from ai_ta_backend.extensions import db

class Base(db.Model):
    __abstract__ = True

class Document(Base):
    __tablename__ = 'documents'
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=func.now())
    s3_path = Column(Text)
    readable_filename = Column(Text)
    course_name = Column(Text)
    url = Column(Text)
    contexts = Column(JSON, default=lambda: [{"text": "", "timestamp": "", "embedding": "", "pagenumber": ""}])
    base_url = Column(Text)

    __table_args__ = (
        Index('documents_course_name_idx', 'course_name', postgresql_using='hash'),
        Index('documents_created_at_idx', 'created_at', postgresql_using='btree'),
        Index('idx_doc_s3_path', 's3_path', postgresql_using='btree'),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "created_at": self.created_at,
            "s3_path": self.s3_path,
            "readable_filename": self.readable_filename,
            "course_name": self.course_name,
            "url": self.url,
            "contexts": self.contexts,
            "base_url": self.base_url
        }

class DocumentDocGroup(Base):
    __tablename__ = 'documents_doc_groups'
    document_id = Column(BigInteger, primary_key=True)
    doc_group_id = Column(BigInteger, ForeignKey('doc_groups.id', ondelete='CASCADE'), primary_key=True)
    created_at = Column(DateTime, default=func.now())

    __table_args__ = (
        Index('documents_doc_groups_doc_group_id_idx', 'doc_group_id', postgresql_using='btree'),
        Index('documents_doc_groups_document_id_idx', 'document_id', postgresql_using='btree'),
    )

class DocGroup(Base):
    __tablename__ = 'doc_groups'
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    course_name = Column(Text, nullable=False)
    created_at = Column(DateTime, default=func.now())
    enabled = Column(Boolean, default=True)
    private = Column(Boolean, default=True)
    doc_count = Column(BigInteger)

    __table_args__ = (Index('doc_groups_enabled_course_name_idx', 'enabled', 'course_name', postgresql_using='btree'),)

class Project(Base):
    __tablename__ = 'projects'
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=func.now())
    course_name = Column(Text)
    doc_map_id = Column(Text)
    convo_map_id = Column(Text)
    n8n_api_key = Column(Text)
    last_uploaded_doc_id = Column(BigInteger)
    last_uploaded_convo_id = Column(BigInteger)
    subscribed = Column(BigInteger, ForeignKey('doc_groups.id', onupdate='CASCADE', ondelete='SET NULL'))
    description = Column(Text)
    metadata_schema = Column(JSON)

    __table_args__ = (
        Index('projects_course_name_key', 'course_name', postgresql_using='btree'),
        Index('projects_pkey', 'id', postgresql_using='btree'),
    )

class N8nWorkflows(Base):
    __tablename__ = 'n8n_workflows'
    latest_workflow_id = Column(BigInteger, primary_key=True, autoincrement=True)
    is_locked = Column(Boolean, nullable=False)

    def __init__(self, is_locked: bool):
        self.is_locked = is_locked

class LlmConvoMonitor(Base):
    __tablename__ = 'llm_convo_monitor'
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=func.now())
    convo = Column(JSON)
    convo_id = Column(Text, unique=True)
    course_name = Column(Text)
    user_email = Column(Text)

    __table_args__ = (
        Index('llm_convo_monitor_course_name_idx', 'course_name', postgresql_using='hash'),
        Index('llm-convo-monitor_convo_id_key', 'convo_id', postgresql_using='btree'),
        Index('llm-convo-monitor_pkey', 'id', postgresql_using='btree'),
    )

class Conversations(Base):
    __tablename__ = 'conversations'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(VARCHAR)
    model = Column(VARCHAR)
    prompt = Column(Text)
    temperature = Column(Float)
    user_email = Column(VARCHAR)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    project_name = Column(Text)
    messages = Column(JSON)
    folder_id = Column(UUID(as_uuid=True))

    __table_args__ = (
        Index('conversations_pkey', 'id', postgresql_using='btree'),
        Index('idx_user_email_updated_at', 'user_email', 'updated_at', postgresql_using='btree'),
    )

class Messages(Base):
    __tablename__ = 'messages'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    conversation_id = Column(UUID(as_uuid=True), ForeignKey('conversations.id', ondelete='CASCADE'))
    role = Column(Text)
    created_at = Column(DateTime, default=func.now())
    contexts = Column(JSON)
    tools = Column(JSON)
    latest_system_message = Column(Text)
    final_prompt_engineered_message = Column(Text)
    response_time_sec = Column(BigInteger)
    content_text = Column(Text)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    content_image_url = Column(Text)
    image_description = Column(Text)

    __table_args__ = (
        Index('messages_pkey', 'id', postgresql_using='btree'),
    )

class PreAuthAPIKeys(Base):
    __tablename__ = 'pre_authorized_api_keys'
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=func.now())  
    emails = Column(JSON)
    providerBodyNoModels = Column(JSON)
    providerName = Column(Text)
    notes = Column(Text)   

    __table_args__ = (
        Index('pre-authorized-api-keys_pkey', 'id', postgresql_using='btree'),
    )