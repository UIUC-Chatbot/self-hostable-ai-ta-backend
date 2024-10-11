import logging
import os
import time
from typing import List
from urllib.parse import quote_plus

from dotenv import load_dotenv
from flask import abort
from flask import Flask
from flask import jsonify
from flask import make_response
from flask import request
from flask import Response
from flask import send_from_directory
from flask_cors import CORS
from flask_executor import Executor
from flask_injector import FlaskInjector
from flask_injector import RequestScope
from injector import Binder
from injector import SingletonScope
from langchain_core.messages import HumanMessage
from langchain_core.messages import SystemMessage
from sqlalchemy import inspect
import urllib3

from ai_ta_backend.database.aws import AWSStorage
from ai_ta_backend.database.poi_sql import POISQLDatabase
from ai_ta_backend.database.qdrant import VectorDatabase
from ai_ta_backend.database.sql import SQLAlchemyDatabase
from ai_ta_backend.executors.flask_executor import ExecutorInterface
from ai_ta_backend.executors.flask_executor import FlaskExecutorAdapter
from ai_ta_backend.executors.process_pool_executor import \
    ProcessPoolExecutorAdapter
from ai_ta_backend.executors.process_pool_executor import \
    ProcessPoolExecutorInterface
from ai_ta_backend.executors.thread_pool_executor import \
    ThreadPoolExecutorAdapter
from ai_ta_backend.executors.thread_pool_executor import \
    ThreadPoolExecutorInterface
from ai_ta_backend.extensions import db
from ai_ta_backend.service.export_service import ExportService
from ai_ta_backend.service.nomic_service import NomicService
from ai_ta_backend.service.poi_agent_service_v2 import POIAgentService
from ai_ta_backend.service.posthog_service import PosthogService
from ai_ta_backend.service.retrieval_service import RetrievalService
from ai_ta_backend.service.sentry_service import SentryService
from ai_ta_backend.service.workflow_service import WorkflowService
from ai_ta_backend.service.sql_alchemy_service import SQLAlchemyService

app = Flask(__name__)
CORS(app)
executor = Executor(app)
# app.config['EXECUTOR_MAX_WORKERS'] = 5 nothing == picks defaults for me
# app.config['SERVER_TIMEOUT'] = 1000  # seconds

# load API keys from globally-availabe .env file
load_dotenv(override=True)


@app.route('/')
def index() -> Response:
  """_summary_

  Args:
      test (int, optional): _description_. Defaults to 1.

  Returns:
      JSON: _description_
  """
  response = jsonify({"hi there, this is a 404": "Welcome to UIUC.chat backend 🚅 Read the docs here: https://docs.uiuc.chat/ "})
  response.headers.add('Access-Control-Allow-Origin', '*')

  return response


@app.route('/getTopContexts', methods=['POST'])
def getTopContexts(service: RetrievalService) -> Response:
  """Get most relevant contexts for a given search query.
  
  Return value

  ## POST body
  course name (optional) str
      A json response with TBD fields.
  search_query
  token_limit
  doc_groups
  
  Returns
  -------
  JSON
      A json response with TBD fields.
  Metadata fields
  * pagenumber_or_timestamp
  * readable_filename
  * s3_pdf_path
  
  Example: 
  [
    {
      'readable_filename': 'Lumetta_notes', 
      'pagenumber_or_timestamp': 'pg. 19', 
      's3_pdf_path': '/courses/<course>/Lumetta_notes.pdf', 
      'text': 'In FSM, we do this...'
    }, 
  ]

  Raises
  ------
  Exception
      Testing how exceptions are handled.
  """
  data = request.get_json()
  search_query: str = data.get('search_query', '')
  course_name: str = data.get('course_name', '')
  token_limit: int = data.get('token_limit', 3000)
  doc_groups: List[str] = data.get('doc_groups', [])

  logging.info(f"QDRANT URL {os.environ['QDRANT_URL']}")
  logging.info(f"QDRANT_API_KEY {os.environ['QDRANT_API_KEY']}")

  if search_query == '' or course_name == '':
    # proper web error "400 Bad request"
    abort(
        400,
        description=
        f"Missing one or more required parameters: 'search_query' and 'course_name' must be provided. Search query: `{search_query}`, Course name: `{course_name}`"
    )

  found_documents = service.getTopContexts(search_query, course_name, token_limit, doc_groups)

  response = jsonify(found_documents)
  response.headers.add('Access-Control-Allow-Origin', '*')
  return response


@app.route('/getAll', methods=['GET'])
def getAll(service: RetrievalService) -> Response:
  """Get all course materials based on the course_name
  """
  logging.info("In getAll()")
  course_name: List[str] | str = request.args.get('course_name', default='', type=str)

  if course_name == '':
    # proper web error "400 Bad request"
    abort(400, description=f"Missing the one required parameter: 'course_name' must be provided. Course name: `{course_name}`")

  distinct_dicts = service.getAll(course_name)

  response = jsonify({"distinct_files": distinct_dicts})
  response.headers.add('Access-Control-Allow-Origin', '*')
  return response


@app.route('/delete', methods=['DELETE'])
def delete(service: RetrievalService, flaskExecutor: ExecutorInterface):
  """
  Delete a single file from all our database: S3, Qdrant, and Supabase (for now).
  Note, of course, we still have parts of that file in our logs.
  """
  course_name: str = request.args.get('course_name', default='', type=str)
  s3_path: str = request.args.get('s3_path', default='', type=str)
  source_url: str = request.args.get('url', default='', type=str)

  if course_name == '' or (s3_path == '' and source_url == ''):
    # proper web error "400 Bad request"
    abort(
        400,
        description=
        f"Missing one or more required parameters: 'course_name' and ('s3_path' or 'source_url') must be provided. Course name: `{course_name}`, S3 path: `{s3_path}`, source_url: `{source_url}`"
    )

  start_time = time.monotonic()
  # background execution of tasks!!
  flaskExecutor.submit(service.delete_data, course_name, s3_path, source_url)
  logging.info(f"From {course_name}, deleted file: {s3_path}")
  logging.info(f"⏰ Runtime of FULL delete func: {(time.monotonic() - start_time):.2f} seconds")
  # we need instant return. Delets are "best effort" assume always successful... sigh :(
  response = jsonify({"outcome": 'success'})
  response.headers.add('Access-Control-Allow-Origin', '*')
  return response


@app.route('/getNomicMap', methods=['GET'])
def nomic_map(service: NomicService):
  course_name: str = request.args.get('course_name', default='', type=str)
  map_type: str = request.args.get('map_type', default='conversation', type=str)

  if course_name == '':
    # proper web error "400 Bad request"
    abort(400, description=f"Missing required parameter: 'course_name' must be provided. Course name: `{course_name}`")

  map_id = service.get_nomic_map(course_name, map_type)
  logging.info("nomic map\n", map_id)

  response = jsonify(map_id)
  response.headers.add('Access-Control-Allow-Origin', '*')
  return response


# @app.route('/createDocumentMap', methods=['GET'])
# def createDocumentMap(service: NomicService):
#   course_name: str = request.args.get('course_name', default='', type=str)

#   if course_name == '':
#     # proper web error "400 Bad request"
#     abort(400, description=f"Missing required parameter: 'course_name' must be provided. Course name: `{course_name}`")

#   map_id = create_document_map(course_name)

#   response = jsonify(map_id)
#   response.headers.add('Access-Control-Allow-Origin', '*')
#   return response


@app.route('/createConversationMap', methods=['GET'])
def createConversationMap(service: NomicService):
  course_name: str = request.args.get('course_name', default='', type=str)

  if course_name == '':
    # proper web error "400 Bad request"
    abort(400, description=f"Missing required parameter: 'course_name' must be provided. Course name: `{course_name}`")

  map_id = service.create_conversation_map(course_name)

  response = jsonify(map_id)
  response.headers.add('Access-Control-Allow-Origin', '*')
  return response


@app.route('/query_sql_agent', methods=['POST'])
def query_sql_agent(service: POIAgentService):
  data = request.get_json()
  user_input = data["query"]
  system_message = SystemMessage(
      content=
      "you are a helpful assistant and need to provide answers in text format about the plants found in India. If the Question is not related to plants in India answer 'I do not have any information on this.'"
  )

  if not user_input:
    return jsonify({"error": "No query provided"}), 400

  try:
    user_01 = HumanMessage(content=user_input)
    inputs = {"messages": [system_message, user_01]}
    response = service.run_workflow(inputs)
    return str(response), 200
  except Exception as e:
    return jsonify({"error": str(e)}), 500


@app.route('/logToConversationMap', methods=['GET'])
def logToConversationMap(service: NomicService, flaskExecutor: ExecutorInterface):
  course_name: str = request.args.get('course_name', default='', type=str)

  if course_name == '':
    # proper web error "400 Bad request"
    abort(400, description=f"Missing required parameter: 'course_name' must be provided. Course name: `{course_name}`")

  #map_id = service.log_to_conversation_map(course_name)
  map_id = flaskExecutor.submit(service.log_to_conversation_map, course_name).result()

  response = jsonify(map_id)
  response.headers.add('Access-Control-Allow-Origin', '*')
  return response


@app.route('/onResponseCompletion', methods=['POST'])
def logToNomic(service: NomicService, flaskExecutor: ExecutorInterface):
  data = request.get_json()
  course_name = data['course_name']
  conversation = data['conversation']

  if course_name == '' or conversation == '':
    # proper web error "400 Bad request"
    abort(
        400,
        description=
        f"Missing one or more required parameters: 'course_name' and 'conversation' must be provided. Course name: `{course_name}`, Conversation: `{conversation}`"
    )
  logging.info(f"In /onResponseCompletion for course: {course_name}")

  # background execution of tasks!!
  #response = flaskExecutor.submit(service.log_convo_to_nomic, course_name, data)
  flaskExecutor.submit(service.log_to_conversation_map, course_name, conversation).result()
  response = jsonify({'outcome': 'success'})
  response.headers.add('Access-Control-Allow-Origin', '*')
  return response


@app.route('/export-convo-history-csv', methods=['GET'])
def export_convo_history(service: ExportService):
  course_name: str = request.args.get('course_name', default='', type=str)
  from_date: str = request.args.get('from_date', default='', type=str)
  to_date: str = request.args.get('to_date', default='', type=str)

  if course_name == '':
    # proper web error "400 Bad request"
    abort(400, description=f"Missing required parameter: 'course_name' must be provided. Course name: `{course_name}`")

  export_status = service.export_convo_history_json(course_name, from_date, to_date)
  logging.info("EXPORT FILE LINKS: ", export_status)

  if export_status['response'] == "No data found between the given dates.":
    response = Response(status=204)
    response.headers.add('Access-Control-Allow-Origin', '*')

  elif export_status['response'] == "Download from S3":
    response = jsonify({"response": "Download from S3", "s3_path": export_status['s3_path']})
    response.headers.add('Access-Control-Allow-Origin', '*')

  else:
    response = make_response(send_from_directory(export_status['response'][2], export_status['response'][1], as_attachment=True))
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers["Content-Disposition"] = f"attachment; filename={export_status['response'][1]}"
    os.remove(export_status['response'][0])

  return response


@app.route('/export-conversations-custom', methods=['GET'])
def export_conversations_custom(service: ExportService):
  course_name: str = request.args.get('course_name', default='', type=str)
  from_date: str = request.args.get('from_date', default='', type=str)
  to_date: str = request.args.get('to_date', default='', type=str)
  emails: str = request.args.getlist('destination_emails_list')

  if course_name == '' and emails == []:
    # proper web error "400 Bad request"
    abort(400, description="Missing required parameter: 'course_name' and 'destination_email_ids' must be provided.")

  export_status = service.export_conversations(course_name, from_date, to_date, emails)
  logging.info("EXPORT FILE LINKS: ", export_status)

  if export_status['response'] == "No data found between the given dates.":
    response = Response(status=204)
    response.headers.add('Access-Control-Allow-Origin', '*')

  elif export_status['response'] == "Download from S3":
    response = jsonify({"response": "Download from S3", "s3_path": export_status['s3_path']})
    response.headers.add('Access-Control-Allow-Origin', '*')

  else:
    response = make_response(send_from_directory(export_status['response'][2], export_status['response'][1], as_attachment=True))
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers["Content-Disposition"] = f"attachment; filename={export_status['response'][1]}"
    os.remove(export_status['response'][0])

  return response


@app.route('/exportDocuments', methods=['GET'])
def exportDocuments(service: ExportService):
  course_name: str = request.args.get('course_name', default='', type=str)
  from_date: str = request.args.get('from_date', default='', type=str)
  to_date: str = request.args.get('to_date', default='', type=str)

  if course_name == '':
    # proper web error "400 Bad request"
    abort(400, description=f"Missing required parameter: 'course_name' must be provided. Course name: `{course_name}`")

  export_status = service.export_documents_json(course_name, from_date, to_date)
  logging.info("EXPORT FILE LINKS: ", export_status)

  if export_status['response'] == "No data found between the given dates.":
    response = Response(status=204)
    response.headers.add('Access-Control-Allow-Origin', '*')

  elif export_status['response'] == "Download from S3":
    response = jsonify({"response": "Download from S3", "s3_path": export_status['s3_path']})
    response.headers.add('Access-Control-Allow-Origin', '*')

  else:
    response = make_response(send_from_directory(export_status['response'][2], export_status['response'][1], as_attachment=True))
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers["Content-Disposition"] = f"attachment; filename={export_status['response'][1]}"
    os.remove(export_status['response'][0])

  return response


@app.route('/getTopContextsWithMQR', methods=['GET'])
def getTopContextsWithMQR(service: RetrievalService, posthog_service: PosthogService) -> Response:
  """
  Get relevant contexts for a given search query, using Multi-query retrieval + filtering method.
  """
  search_query: str = request.args.get('search_query', default='', type=str)
  course_name: str = request.args.get('course_name', default='', type=str)
  token_limit: int = request.args.get('token_limit', default=3000, type=int)
  if search_query == '' or course_name == '':
    # proper web error "400 Bad request"
    abort(
        400,
        description=
        f"Missing one or more required parameters: 'search_query' and 'course_name' must be provided. Search query: `{search_query}`, Course name: `{course_name}`"
    )

  posthog_service.capture(event_name='filter_top_contexts_invoked',
                          properties={
                              'user_query': search_query,
                              'course_name': course_name,
                              'token_limit': token_limit,
                          })

  found_documents = service.getTopContextsWithMQR(search_query, course_name, token_limit)

  response = jsonify(found_documents)
  response.headers.add('Access-Control-Allow-Origin', '*')
  return response


@app.route('/getworkflows', methods=['GET'])
def get_all_workflows(service: WorkflowService) -> Response:
  """
  Get all workflows from user.
  """

  api_key = request.args.get('api_key', default='', type=str)
  limit = request.args.get('limit', default=100, type=int)
  pagination = request.args.get('pagination', default=True, type=bool)
  active = request.args.get('active', default=False, type=bool)
  name = request.args.get('workflow_name', default='', type=str)
  logging.info(request.args)

  logging.info("In get_all_workflows.. api_key: ", api_key)

  # if no API Key, return empty set.
  # if api_key == '':
  #   # proper web error "400 Bad request"
  #   abort(400, description=f"Missing N8N API_KEY: 'api_key' must be provided. Search query: `{api_key}`")

  try:
    response = service.get_workflows(limit, pagination, api_key, active, name)
    response = jsonify(response)
    response.headers.add('Access-Control-Allow-Origin', '*')
    return response
  except Exception as e:
    if "unauthorized" in str(e).lower():
      logging.info("Unauthorized error in get_all_workflows: ", e)
      abort(401, description=f"Unauthorized: 'api_key' is invalid. Search query: `{api_key}`")
    else:
      logging.info("Error in get_all_workflows: ", e)
      abort(500, description=f"Failed to fetch n8n workflows: {e}")


@app.route('/switch_workflow', methods=['GET'])
def switch_workflow(service: WorkflowService) -> Response:
  """
  Activate or deactivate flow for user.
  """

  api_key = request.args.get('api_key', default='', type=str)
  activate = request.args.get('activate', default='', type=str)
  id = request.args.get('id', default='', type=str)

  logging.info(request.args)

  if api_key == '':
    # proper web error "400 Bad request"
    abort(400, description=f"Missing N8N API_KEY: 'api_key' must be provided. Search query: `{api_key}`")

  try:
    logging.info("activation!!!!!!!!!!!", activate)
    response = service.switch_workflow(id, api_key, activate)
    response = jsonify(response)
    response.headers.add('Access-Control-Allow-Origin', '*')
    return response
  except Exception as e:
    if e == "Unauthorized":
      abort(401, description=f"Unauthorized: 'api_key' is invalid. Search query: `{api_key}`")
    else:
      abort(400, description=f"Bad request: {e}")


@app.route('/run_flow', methods=['POST'])
def run_flow(service: WorkflowService) -> Response:
  """
  Run flow for a user and return results.
  """

  api_key = request.json.get('api_key', '')
  name = request.json.get('name', '')
  data = request.json.get('data', '')

  logging.info("Got /run_flow request:", request.json)

  if api_key == '':
    # proper web error "400 Bad request"
    abort(400, description=f"Missing N8N API_KEY: 'api_key' must be provided. Search query: `{api_key}`")

  try:
    response = service.main_flow(name, api_key, data)
    response = jsonify(response)
    response.headers.add('Access-Control-Allow-Origin', '*')
    return response
  except Exception as e:
    if e == "Unauthorized":
      abort(401, description=f"Unauthorized: 'api_key' is invalid. Search query: `{api_key}`")
    else:
      abort(400, description=f"Bad request: {e}")


def configure(binder: Binder) -> None:
  vector_bound = False
  sql_bound = False
  storage_bound = False

  # Define database URLs with conditional checks for environment variables
  encoded_password = quote_plus(os.getenv('SUPABASE_PASSWORD'))
  DB_URLS = {
      'supabase':
          f"postgresql://{os.getenv('SUPABASE_USER')}:{encoded_password}@{os.getenv('SUPABASE_URL')}",
      'sqlite':
          f"sqlite:///{os.getenv('SQLITE_DB_NAME')}" if os.getenv('SQLITE_DB_NAME') else None,
      'postgres':
          f"postgresql://{os.getenv('POSTGRES_USER')}:{os.getenv('POSTGRES_PASSWORD')}@{os.getenv('POSTGRES_URL')}"
          if os.getenv('POSTGRES_USER') and os.getenv('POSTGRES_PASSWORD') and os.getenv('POSTGRES_URL') else None
  }

  # Bind to the first available SQL database configuration
  for db_type, url in DB_URLS.items():
    if url:
      logging.info(f"Binding to {db_type} database with URL: {url}")
      with app.app_context():
        app.config['SQLALCHEMY_DATABASE_URI'] = url
        db.init_app(app)

        # Check if tables exist before creating them
        inspector = inspect(db.engine)
        existing_tables = inspector.get_table_names()

        if not existing_tables:
          logging.info("Creating tables as the database is empty")
          db.create_all()
        else:
          logging.info("Tables already exist, skipping creation")

      binder.bind(SQLAlchemyDatabase, to=SQLAlchemyDatabase(db), scope=SingletonScope)
      sql_bound = True
      break

  if os.getenv("POI_SQL_DB_NAME"):
    logging.info(f"Binding to POI SQL database with URL: {os.getenv('POI_SQL_DB_NAME')}")
    binder.bind(POISQLDatabase, to=POISQLDatabase(db), scope=SingletonScope)
    binder.bind(POIAgentService, to=POIAgentService, scope=SingletonScope)
  # Conditionally bind databases based on the availability of their respective secrets
  if all(os.getenv(key) for key in ["QDRANT_URL", "QDRANT_API_KEY", "QDRANT_COLLECTION_NAME"]) or any(
      os.getenv(key) for key in ["PINECONE_API_KEY", "PINECONE_PROJECT_NAME"]):
    logging.info("Binding to Qdrant database")

    logging.info(f"Qdrant Collection Name: {os.environ['QDRANT_COLLECTION_NAME']}")
    logging.info(f"Qdrant URL: {os.environ['QDRANT_URL']}")
    logging.info(f"Qdrant API Key: {os.environ['QDRANT_API_KEY']}")
    binder.bind(VectorDatabase, to=VectorDatabase, scope=SingletonScope)
    vector_bound = True

  if all(os.getenv(key) for key in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "S3_BUCKET_NAME"]) or any(
      os.getenv(key) for key in ["MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "MINIO_URL"]):
    if os.getenv("AWS_ACCESS_KEY_ID") and os.getenv("AWS_SECRET_ACCESS_KEY"):
      logging.info("Binding to AWS storage")
    elif os.getenv("MINIO_ACCESS_KEY") and os.getenv("MINIO_SECRET_KEY"):
      logging.info("Binding to Minio storage")
    binder.bind(AWSStorage, to=AWSStorage, scope=SingletonScope)
    storage_bound = True

  # Conditionally bind services based on the availability of their respective secrets
  if os.getenv("NOMIC_API_KEY"):
    logging.info("Binding to Nomic service")
    binder.bind(NomicService, to=NomicService, scope=SingletonScope)

  if os.getenv("POSTHOG_API_KEY"):
    logging.info("Binding to Posthog service")
    binder.bind(PosthogService, to=PosthogService, scope=SingletonScope)

  if os.getenv("SENTRY_DSN"):
    logging.info("Binding to Sentry service")
    binder.bind(SentryService, to=SentryService, scope=SingletonScope)

  if os.getenv("EMAIL_SENDER"):
    logging.info("Binding to Export service")
    binder.bind(ExportService, to=ExportService, scope=SingletonScope)

  if os.getenv("N8N_URL"):
    logging.info("Binding to Workflow service")
    binder.bind(WorkflowService, to=WorkflowService, scope=SingletonScope)

  if vector_bound and sql_bound and storage_bound:
    logging.info("Binding to Retrieval service")
    binder.bind(RetrievalService, to=RetrievalService, scope=RequestScope)

  # Always bind the executor and its adapters
  binder.bind(ExecutorInterface, to=FlaskExecutorAdapter(executor), scope=SingletonScope)
  binder.bind(ThreadPoolExecutorInterface, to=ThreadPoolExecutorAdapter, scope=SingletonScope)
  binder.bind(ProcessPoolExecutorInterface, to=ProcessPoolExecutorAdapter, scope=SingletonScope)
  logging.info("Configured all services and adapters", binder._bindings)


# New endpoints for SQLAlchemyDatabase methods


@app.route('/getAllMaterialsForCourse', methods=['GET'])
def get_all_materials_for_course(db: SQLAlchemyDatabase, service: SQLAlchemyService):
  course_name = request.args.get('course_name', '')
  if not course_name:
    abort(400, description="Missing required parameter: 'course_name'")

  result = service.getAllMaterialsForCourse(course_name=course_name)
  print(f"result of get_all_materials_for_course: {result}")

  return make_response(jsonify(result), 200)


@app.route('/getMaterialsForCourseAndS3Path', methods=['GET'])
def get_materials_for_course_and_s3_path(db: SQLAlchemyDatabase):
  course_name = request.args.get('course_name', '')
  s3_path = request.args.get('s3_path', '')
  if not course_name or not s3_path:
    abort(400, description="Missing required parameters: 'course_name' and 's3_path'")
  result = db.getMaterialsForCourseAndS3Path(course_name, s3_path)
  return jsonify(result)


@app.route('/getMaterialsForCourseAndKeyAndValue', methods=['GET'])
def get_materials_for_course_and_key_and_value(db: SQLAlchemyDatabase):
  course_name = request.args.get('course_name', '')
  key = request.args.get('key', '')
  value = request.args.get('value', '')
  if not course_name or not key or not value:
    abort(400, description="Missing required parameters: 'course_name', 'key', and 'value'")
  result = db.getMaterialsForCourseAndKeyAndValue(course_name, key, value)
  return jsonify(result)


@app.route('/deleteMaterialsForCourseAndKeyAndValue', methods=['DELETE'])
def delete_materials_for_course_and_key_and_value(db: SQLAlchemyDatabase):
  course_name = request.args.get('course_name', '')
  key = request.args.get('key', '')
  value = request.args.get('value', '')
  if not course_name or not key or not value:
    abort(400, description="Missing required parameters: 'course_name', 'key', and 'value'")
  db.deleteMaterialsForCourseAndKeyAndValue(course_name, key, value)
  return jsonify({"status": "success"})


@app.route('/deleteMaterialsForCourseAndS3Path', methods=['DELETE'])
def delete_materials_for_course_and_s3_path(db: SQLAlchemyDatabase):
  course_name = request.args.get('course_name', '')
  s3_path = request.args.get('s3_path', '')
  if not course_name or not s3_path:
    abort(400, description="Missing required parameters: 'course_name' and 's3_path'")
  db.deleteMaterialsForCourseAndS3Path(course_name, s3_path)
  return jsonify({"status": "success"})


@app.route('/getDocumentsBetweenDates', methods=['GET'])
def get_documents_between_dates(db: SQLAlchemyDatabase):
  course_name = request.args.get('course_name', '')
  from_date = request.args.get('from_date', '')
  to_date = request.args.get('to_date', '')
  table_name = request.args.get('table_name', '')
  if not course_name or not table_name:
    abort(400, description="Missing required parameters: 'course_name' and 'table_name'")
  result = db.getDocumentsBetweenDates(course_name, from_date, to_date, table_name)
  return jsonify(result)


@app.route('/getAllDocumentsForDownload', methods=['GET'])
def get_all_documents_for_download(db: SQLAlchemyDatabase):
  course_name = request.args.get('course_name', '')
  first_id = request.args.get('first_id', type=int)
  if not course_name or first_id is None:
    abort(400, description="Missing required parameters: 'course_name' and 'first_id'")
  result = db.getAllDocumentsForDownload(course_name, first_id)
  return jsonify(result)


@app.route('/getDocsForIdsGte', methods=['GET'])
def get_docs_for_ids_gte(db: SQLAlchemyDatabase):
  course_name = request.args.get('course_name', '')
  first_id = request.args.get('first_id', type=int)
  fields = request.args.get('fields', '*')
  limit = request.args.get('limit', 100, type=int)
  if not course_name or first_id is None:
    abort(400, description="Missing required parameters: 'course_name' and 'first_id'")
  result = db.getDocsForIdsGte(course_name, first_id, fields, limit)
  return jsonify(result)


@app.route('/getProjectsMapForCourse', methods=['GET'])
def get_projects_map_for_course(db: SQLAlchemyDatabase):
  course_name = request.args.get('course_name', '')
  if not course_name:
    abort(400, description="Missing required parameter: 'course_name'")
  result = db.getProjectsMapForCourse(course_name)
  return jsonify(result)


@app.route('/insertProjectInfo', methods=['POST'])
def insert_project_info(db: SQLAlchemyDatabase):
  project_info = request.json
  if not project_info:
    abort(400, description="Missing project information in request body")
  db.insertProjectInfo(project_info)
  return jsonify({"status": "success"})


@app.route('/getDocMapFromProjects', methods=['GET'])
def get_doc_map_from_projects(db: SQLAlchemyDatabase):
  course_name = request.args.get('course_name', '')
  if not course_name:
    abort(400, description="Missing required parameter: 'course_name'")
  result = db.getDocMapFromProjects(course_name)
  return jsonify(result)


@app.route('/getConvoMapFromProjects', methods=['GET'])
def get_convo_map_from_projects(db: SQLAlchemyDatabase):
  course_name = request.args.get('course_name', '')
  if not course_name:
    abort(400, description="Missing required parameter: 'course_name'")
  result = db.getConvoMapFromProjects(course_name)
  return jsonify(result)


@app.route('/updateProjects', methods=['PUT'])
def update_projects(db: SQLAlchemyDatabase):
  course_name = request.args.get('course_name', '')
  data = request.json
  if not course_name or not data:
    abort(400, description="Missing required parameter: 'course_name' or update data")
  db.updateProjects(course_name, data)
  return jsonify({"status": "success"})


@app.route('/insertProject', methods=['POST'])
def insert_project(db: SQLAlchemyDatabase):
  project_info = request.json
  if not project_info:
    abort(400, description="Missing project information in request body")
  db.insertProject(project_info)
  return jsonify({"status": "success"})


@app.route('/getAllConversationsForDownload', methods=['GET'])
def get_all_conversations_for_download(db: SQLAlchemyDatabase):
  course_name = request.args.get('course_name', '')
  first_id = request.args.get('first_id', type=int)
  if not course_name or first_id is None:
    abort(400, description="Missing required parameters: 'course_name' and 'first_id'")
  result = db.getAllConversationsForDownload(course_name, first_id)
  return jsonify(result)


@app.route('/getAllConversationsBetweenIds', methods=['GET'])
def get_all_conversations_between_ids(db: SQLAlchemyDatabase):
  course_name = request.args.get('course_name', '')
  first_id = request.args.get('first_id', type=int)
  last_id = request.args.get('last_id', type=int)
  limit = request.args.get('limit', 50, type=int)
  if not course_name or first_id is None:
    abort(400, description="Missing required parameters: 'course_name' and 'first_id'")
  result = db.getAllConversationsBetweenIds(course_name, first_id, last_id, limit)
  return jsonify(result)


@app.route('/getAllFromLLMConvoMonitor', methods=['GET'])
def get_all_from_llm_convo_monitor(db: SQLAlchemyDatabase):
  course_name = request.args.get('course_name', '')
  if not course_name:
    abort(400, description="Missing required parameter: 'course_name'")
  result = db.getAllFromLLMConvoMonitor(course_name)
  return jsonify(result)


@app.route('/getCountFromLLMConvoMonitor', methods=['GET'])
def get_count_from_llm_convo_monitor(db: SQLAlchemyDatabase):
  course_name = request.args.get('course_name', '')
  last_id = request.args.get('last_id', 0, type=int)
  if not course_name:
    abort(400, description="Missing required parameter: 'course_name'")
  result = db.getCountFromLLMConvoMonitor(course_name, last_id)
  return jsonify(result)


@app.route('/getConversation', methods=['GET'])
def get_conversation(db: SQLAlchemyDatabase):
  course_name = request.args.get('course_name', '')
  key = request.args.get('key', '')
  value = request.args.get('value', '')
  if not course_name or not key or not value:
    abort(400, description="Missing required parameters: 'course_name', 'key', and 'value'")
  result = db.getConversation(course_name, key, value)
  return jsonify(result)


@app.route('/getAllConversationsForUserAndProject', methods=['GET'])
def get_all_conversations_for_user_and_project(db: SQLAlchemyDatabase):
  user_email = request.args.get('user_email', '')
  project_name = request.args.get('project_name', '')
  curr_count = request.args.get('curr_count', 0, type=int)
  if not user_email or not project_name:
    abort(400, description="Missing required parameters: 'user_email' and 'project_name'")
  result = db.getAllConversationsForUserAndProject(user_email, project_name, curr_count)
  return jsonify(result)


@app.route('/getDisabledDocGroups', methods=['GET'])
def get_disabled_doc_groups(db: SQLAlchemyDatabase):
  course_name = request.args.get('course_name', '')
  if not course_name:
    abort(400, description="Missing required parameter: 'course_name'")
  result = db.getDisabledDocGroups(course_name)
  return jsonify(result)


@app.route('/getLatestWorkflowId', methods=['GET'])
def get_latest_workflow_id(db: SQLAlchemyDatabase):
  result = db.getLatestWorkflowId()
  return jsonify(result)


@app.route('/lockWorkflow', methods=['POST'])
def lock_workflow(db: SQLAlchemyDatabase):
  id = request.args.get('id', type=int)
  if id is None:
    abort(400, description="Missing required parameter: 'id'")
  db.lockWorkflow(id)
  return jsonify({"status": "success"})


@app.route('/deleteLatestWorkflowId', methods=['DELETE'])
def delete_latest_workflow_id(db: SQLAlchemyDatabase):
  id = request.args.get('id', type=int)
  if id is None:
    abort(400, description="Missing required parameter: 'id'")
  db.deleteLatestWorkflowId(id)
  return jsonify({"status": "success"})


@app.route('/unlockWorkflow', methods=['POST'])
def unlock_workflow(db: SQLAlchemyDatabase):
  id = request.args.get('id', type=int)
  if id is None:
    abort(400, description="Missing required parameter: 'id'")
  db.unlockWorkflow(id)
  return jsonify({"status": "success"})


@app.route('/getPreAssignedAPIKeys', methods=['GET'])
def get_pre_assigned_api_keys(db: SQLAlchemyDatabase):
  email = request.args.get('email', '')
  if not email:
    abort(400, description="Missing required parameter: 'email'")
  result = db.getPreAssignedAPIKeys(email)
  return jsonify(result)


# ... (rest of the code remains unchanged)

FlaskInjector(app=app, modules=[configure])

if __name__ == '__main__':
  app.run(debug=True, port=int(os.getenv("PORT", default=8000)))  # nosec -- reasonable bandit error suppression
