#!/usr/bin/env python3
"""
Jira Work Item Events and Sprint Data Extraction Script

This script extracts work item events and sprint data from Jira and supports two modes of operation:

Usage:
    python extract_jira.py [-p <product_name>] [-s <start_date>]

Arguments:
    -p, --product: Product name (if provided, saves to database; otherwise saves to CSV)
    -s, --start-date: Start date for extraction in YYYY-MM-DD format (optional)

Modes (automatically determined):
    Database mode (when -p is provided):
        - Imports from the database module
        - Connects to the database
        - Reads the config from the data_source_config table
        - Gets the last extraction timestamp from the work_item_event table
        - Saves the extracted data to work_item_event and sprint tables

    CSV mode (when -p is NOT provided):
        - Reads the config from config.json:
          * JIRA_ORGANIZATION: Jira Organization
          * JIRA_USER_EMAIL: Jira User Email
          * JIRA_API_TOKEN: Jira API token
          * JIRA_PROJECTS: Comma separated Jira Project Keys
          * JIRA_WORK_ITEM_ATTRIBUTES: Comma separated Jira Work Item Attributes (Optional)
          * EXPORT_PATH: Export Path (Optional)
        - Gets the last extraction timestamp from the checkpoint (JSON) file
        - Saves the extracted data to two CSV files (jira_events and jira_sprints), updating the checkpoint (JSON) file

Work Item Events extracted:
    - Work Item Created
    - Work Item State Changed
    - Work Item Updated

Sprint Data extracted:
    - Sprint ID, Name, Project, Board Name
    - Created Date, Start Date, End Date, Completion Date
    - Status (future/active/closed)
"""

import requests
import base64
import json
from datetime import datetime
import pytz
import sys
import os
import argparse
import time
from typing import Dict

base_dir = os.path.dirname(os.path.abspath(__file__))
common_dir = os.path.join(base_dir, "common")
if not os.path.isdir(common_dir):
    # go up one level to find "common" (for installed package structure)
    base_dir = os.path.dirname(base_dir)
    common_dir = os.path.join(base_dir, "common")

if os.path.isdir(common_dir) and base_dir not in sys.path:
    sys.path.insert(0, base_dir)

import logging
from common.utils import Utils

# Configure logging to print messages on stdout
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', stream=sys.stdout)
logger = logging.getLogger('extract_jira')

class JiraExtractor:
    """Extracts work item events and sprint events from Jira with page-by-page processing."""

    def __init__(self):
        # Statistics
        self.stats = {
            'work_items_created': 0,
            'work_items_updated': 0,
            'work_items_state_changed': 0,
            'total_inserted': 0,
            'total_duplicates': 0,
            'total_boards': 0,
            'total_sprints': 0,
            'sprints_inserted': 0,
            'sprints_updated': 0
        }

    def get_config_from_database(self, cursor):
        """Get Jira configuration from data_source_config table."""
        query = """
        SELECT config_item, config_value
        FROM data_source_config
        WHERE data_source = 'work_item_management'
        AND config_item IN ('Organization', 'User Email Address', 'Personal Access Token', 'Projects', 'Custom Fields')
        """
        cursor.execute(query)
        results = cursor.fetchall()

        config = {}
        for row in results:
            config_item, config_value = row
            if config_item == 'Projects':
                # Parse the JSON list and get the first project
                try:
                    projects = json.loads(config_value)
                    config['project'] = projects[0] if projects else None
                except (json.JSONDecodeError, IndexError):
                    config['project'] = None
            elif config_item == 'Organization':
                # For Jira, Organization is the API URL (subdomain)
                config['jira_api_url'] = f"https://{config_value}.atlassian.net"
            elif config_item == 'User Email Address':
                config['jira_user_email'] = config_value
            elif config_item == 'Personal Access Token':
                config['jira_api_token'] = config_value
            elif config_item == 'Custom Fields':
                # Parse the JSON list of custom fields
                try:
                    custom_fields = json.loads(config_value) if config_value else []
                    config['jira_custom_fields'] = custom_fields
                except (json.JSONDecodeError, TypeError):
                    config['jira_custom_fields'] = []

        return config

    def get_last_modified_date(self, cursor):
        """Get the last modified date from the database."""
        query = """
        SELECT MAX(timestamp_utc) FROM work_item_event;
        """
        cursor.execute(query)
        result = cursor.fetchone()
        if result[0]:
            # Convert to naive datetime if timezone-aware
            dt = result[0]
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
            return dt
        else:
            return datetime(2000, 1, 1)

    def run_extraction(self, cursor, config: Dict, start_date, last_modified, export_path: str = None):
        """
        Run extraction: fetch and save data page-by-page.

        Args:
            cursor: Database cursor (None for CSV mode)
            config: Configuration dictionary with jira_api_url, jira_user_email, jira_api_token, project/projects
            start_date: Start date string from command line (optional)
            last_modified: Last modified datetime from database or checkpoint
            export_path: Export path for CSV mode
        """
        # Track maximum timestamp for checkpoint saving
        max_timestamp = None

        # Validate required configuration
        if not config.get('jira_api_url'):
            logger.error("Missing required configuration: Jira API URL")
            sys.exit(1)
        if not config.get('jira_user_email'):
            logger.error("Missing required configuration: Jira User Email")
            sys.exit(1)
        if not config.get('jira_api_token'):
            logger.error("Missing required configuration: Jira API Token")
            sys.exit(1)

        # Set up Jira API configuration
        api_url = config['jira_api_url']
        email = config['jira_user_email']
        api_token = config['jira_api_token']
        headers = get_jira_auth_headers(email, api_token)

        # Map custom field names to field IDs
        custom_fields = config.get('jira_custom_fields', [])
        extended_attribute_ids = []
        if custom_fields:
            logger.info(f"Mapping custom fields: {custom_fields}")
            extended_attribute_ids = process_extended_fields(custom_fields, api_url, headers)
            logger.info(f"Mapped {len(extended_attribute_ids)} custom fields to field IDs")

        # Determine the start date
        if start_date:
            try:
                last_modified_date = datetime.strptime(start_date, '%Y-%m-%d')
            except ValueError:
                logger.error("Invalid date format. Please use YYYY-MM-DD format.")
                sys.exit(1)
        else:
            last_modified_date = last_modified
            # Convert to naive datetime if timezone-aware
            if last_modified_date and last_modified_date.tzinfo is not None:
                last_modified_date = last_modified_date.replace(tzinfo=None)

        # Set up function wrappers to avoid repeated if checks
        if cursor:
            # Database mode
            def save_output_fn(events):
                return self.save_events_to_database(events, cursor)
            def save_sprint_output_fn(sprints):
                return self.save_sprints_to_database(sprints, cursor)
            projects = [config.get('project')] if config.get('project') else []
        else:
            # CSV mode - create CSV files at the start
            csv_file = Utils.create_csv_file("jira_events", export_path, logger)
            sprint_csv_file = Utils.create_csv_file("jira_sprints", export_path, logger)
            def save_output_fn(events):
                result = Utils.save_events_to_csv(events, csv_file, logger)
                nonlocal max_timestamp
                if result[3] and (not max_timestamp or result[3] > max_timestamp):
                    max_timestamp = result[3]
                return result[1:3]  # Return only inserted and duplicates
            def save_sprint_output_fn(sprints):
                result = Utils.save_events_to_csv(sprints, sprint_csv_file, logger)
                return result[1:3]  # Return only inserted and duplicates
            projects = config.get('projects', [])

        # Validate projects
        if not projects:
            logger.error("No projects configured")
            sys.exit(1)

        # Log the fetch information
        if last_modified_date is not None:
            logger.info(f"Starting extraction from {last_modified_date}")
        logger.info(f"Fetching data from {api_url}")
        logger.info(f"Projects: {', '.join(projects)}")

        # Process each project
        for project in projects:
            if not project or not project.strip():
                continue

            logger.info(f"Processing project: {project.strip()}")

            try:
                # Process work item events
                logger.info(f"Extracting work item events for project: {project.strip()}")
                # Create a callback function for per-page processing
                def process_page_callback(issues):
                    return process_issues_page(issues, last_modified_date, api_url, headers, save_output_fn, extended_attribute_ids)

                total_inserted, total_duplicates = fetch_jira_issues(
                    api_url, project.strip(), last_modified_date, None, headers, extended_attribute_ids, process_page_callback
                )

                logger.info(f"Project {project.strip()} work items completed. Total inserted: {total_inserted}, skipped: {total_duplicates} duplicates")
                self.stats['total_inserted'] += total_inserted
                self.stats['total_duplicates'] += total_duplicates

            except Exception as e:
                logger.error(f"Error fetching issues from project {project.strip()}: {str(e)}")
                continue

            try:
                # Process sprints
                logger.info(f"Extracting sprints for project: {project.strip()}")
                # Fetch boards for the project
                boards = fetch_jira_boards(api_url, project.strip(), headers)
                self.stats['total_boards'] += len(boards)
                logger.info(f"Found {len(boards)} board(s) for project {project.strip()}")

                # Process each board
                for board in boards:
                    board_id = board.get('id')
                    board_name = board.get('name')
                    board_project_key = board.get('location', {}).get('projectKey', project.strip())
                    logger.info(f"Processing board: {board_name} (ID: {board_id}, Project: {board_project_key})")

                    # Fetch sprints for the board
                    sprints = fetch_board_sprints(api_url, board_id, headers)
                    logger.info(f"Found {len(sprints)} sprint(s) for board {board_name}")

                    # Filter to keep only sprints that originated from this board
                    sprints = [s for s in sprints if s.get('originBoardId') == board_id]
                    logger.info(f"Filtered to {len(sprints)} sprint(s) with origin board ID {board_id}")

                    # Process and save sprints
                    if sprints:
                        sprint_data = process_sprints(sprints, board_name, board_project_key)
                        self.stats['total_sprints'] += len(sprint_data)

                        if sprint_data:
                            inserted, updated = save_sprint_output_fn(sprint_data)
                            self.stats['sprints_inserted'] += inserted
                            self.stats['sprints_updated'] += updated
                            logger.info(f"Board {board_name}: inserted {inserted}, updated {updated} sprints")

            except Exception as e:
                logger.error(f"Error processing sprints for project {project.strip()}: {str(e)}")
                continue

        # Save checkpoint in CSV mode
        if not cursor and max_timestamp:
            if Utils.save_checkpoint(prefix="jira", last_dt=max_timestamp, export_path=export_path):
                logger.info(f"Checkpoint saved successfully: {max_timestamp}")
            else:
                logger.warning("Failed to save checkpoint")

        # Print summary statistics
        logger.info(f"=== Work Item Events Summary ===")
        logger.info(f"Inserted {self.stats['total_inserted']} records, skipped {self.stats['total_duplicates']} duplicate records.")
        logger.info(f"=== Sprint Summary ===")
        logger.info(f"Boards: {self.stats['total_boards']}, Sprints: {self.stats['total_sprints']}")
        logger.info(f"Inserted: {self.stats['sprints_inserted']}, Updated: {self.stats['sprints_updated']}")

    def save_events_to_database(self, events, cursor):
        """Save events to database."""
        if not events:
            return 0, 0

        from psycopg2.extras import execute_values

        # Define the columns for the insert
        columns = [
            'work_item_id', 'project', 'type', 'parent_work_item_id',
            'state', 'title', 'timestamp_utc', 'event', 'extended_attributes', 'description_revisions'
        ]

        # Get count before insertion
        cursor.execute("SELECT COUNT(*) FROM work_item_event")
        count_before = cursor.fetchone()[0]

        # Prepare data for batch insertion
        insert_data = []
        for event in events:
            insert_data.append((
                event.get("work_item_id"),
                event.get("project"),
                event.get("type"),
                event.get("parent_work_item_id"),
                event.get("state"),
                event.get("title"),
                event.get("timestamp_utc"),
                event.get("event"),
                event.get("extended_attributes"),
                event.get("description_revisions", 0)
            ))

        # Use execute_values for batch insertion
        execute_values(
            cursor,
            f"INSERT INTO work_item_event ({', '.join(columns)}) VALUES %s ON CONFLICT DO NOTHING",
            insert_data,
            template=None,
            page_size=1000
        )

        # Get count after insertion to determine actual inserted records
        cursor.execute("SELECT COUNT(*) FROM work_item_event")
        count_after = cursor.fetchone()[0]

        # Calculate actual inserted and skipped records
        inserted_count = count_after - count_before
        duplicate_count = len(insert_data) - inserted_count

        return inserted_count, duplicate_count

    def save_sprints_to_database(self, sprints, cursor):
        """Save sprints to database with UPSERT logic."""
        if not sprints:
            return 0, 0

        # Get product_id from session configuration
        cursor.execute("SELECT current_setting('pa.product_id', true)")
        product_id_result = cursor.fetchone()
        product_id = int(product_id_result[0]) if product_id_result and product_id_result[0] else None

        if product_id is None:
            logger.error("product_id not set in session configuration")
            return 0, 0

        inserted_count = 0
        updated_count = 0

        for sprint in sprints:
            # Check if sprint exists
            cursor.execute(
                "SELECT id FROM sprint WHERE product_id = %s AND sprint_id = %s",
                (product_id, sprint.get("sprint_id"))
            )
            exists = cursor.fetchone()

            if exists:
                # Update existing sprint
                cursor.execute(
                    """
                    UPDATE sprint 
                    SET sprint_name = %s, project = %s, board_name = %s, 
                        created_date_utc = %s, start_date_utc = %s, end_date_utc = %s, 
                        completion_date_utc = %s, status = %s
                    WHERE product_id = %s AND sprint_id = %s
                    """,
                    (
                        sprint.get("sprint_name"),
                        sprint.get("project"),
                        sprint.get("board_name"),
                        sprint.get("created_date_utc"),
                        sprint.get("start_date_utc"),
                        sprint.get("end_date_utc"),
                        sprint.get("completion_date_utc"),
                        sprint.get("status"),
                        product_id,
                        sprint.get("sprint_id")
                    )
                )
                updated_count += 1
            else:
                # Insert new sprint
                cursor.execute(
                    """
                    INSERT INTO sprint (
                        product_id, sprint_id, sprint_name, project, board_name,
                        created_date_utc, start_date_utc, end_date_utc, completion_date_utc, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        product_id,
                        sprint.get("sprint_id"),
                        sprint.get("sprint_name"),
                        sprint.get("project"),
                        sprint.get("board_name"),
                        sprint.get("created_date_utc"),
                        sprint.get("start_date_utc"),
                        sprint.get("end_date_utc"),
                        sprint.get("completion_date_utc"),
                        sprint.get("status")
                    )
                )
                inserted_count += 1

        return inserted_count, updated_count

# Function to safely extract data and handle missing or invalid fields
def safe_get(data, keys, default_value='NULL'):
    try:
        for key in keys:
            data = data[key]
        if isinstance(data, str):
            return "'{}'".format(data.replace("'", "''"))  # Escape single quotes for SQL
        if isinstance(data, (int, float)):
            return data
        return "'{}'".format(data)
    except (KeyError, TypeError):
        return default_value


# Function to build extended_attributes JSON with Jira-specific fields
def build_extended_attributes(issue_fields, extended_attribute_ids):
    """
    Build extended_attributes JSON including Jira-specific fields.
    Sprint field is excluded as it will be populated from changelog.
    """
    extended_attrs = {}

    # Standard Jira fields
    assignee_value = issue_fields.get('assignee', {}).get('displayName') if isinstance(issue_fields.get('assignee'), dict) else issue_fields.get('assignee')
    if assignee_value is not None:
        extended_attrs['assignee'] = assignee_value

    labels_value = issue_fields.get('labels', [])
    if labels_value:
        extended_attrs['labels'] = ', '.join(labels_value)

    # Add custom extended attributes if provided
    if extended_attribute_ids:
        for field_name, field_id in extended_attribute_ids:
            # Special handling for Sprint field - extract to all_sprints key
            if field_name.lower() == 'sprint':
                field_key = 'all_sprints'
            else:
                field_key = field_name.replace(" ", "_").lower()
            
            field_value = issue_fields.get(field_id)
            
            if field_value is not None:
                if isinstance(field_value, dict):
                    # Extract first available key: value, name, or displayName
                    extended_attrs[field_key] = (
                        field_value.get('value') or field_value.get('name') or 
                        field_value.get('displayName') or field_value
                    )
                elif isinstance(field_value, list):
                    processed_values = []
                    for item in field_value:
                        if isinstance(item, dict):
                            # Extract first available key: value, name, or displayName
                            processed_values.append(
                                item.get('value') or item.get('name') or 
                                item.get('displayName') or item
                            )
                        else:
                            processed_values.append(item)
                    extended_attrs[field_key] = processed_values
                else:
                    extended_attrs[field_key] = field_value

    return json.dumps(extended_attrs) if extended_attrs else None

# Function to check if Title/Description changed
def has_content_changed(changelog_items):
    """
    Check if Title or Description changed in this changelog entry.
    Returns 1 if any of these fields changed, 0 otherwise.
    """
    # Fields to track for changes (Jira field names)
    content_fields = [
        'summary',  # Title in Jira
        'description',  # Description in Jira
    ]

    for item in changelog_items:
        field_name = item.get("field", "").lower()
        if field_name in [f.lower() for f in content_fields]:
            return 1

    return 0

# Function to get Jira authentication headers
def get_jira_auth_headers(email, api_token):
    """Return headers for Jira API authentication."""
    auth_str = f"{email}:{api_token}"
    auth_bytes = auth_str.encode('ascii')
    base64_bytes = base64.b64encode(auth_bytes)
    base64_auth = base64_bytes.decode('ascii')
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Basic {base64_auth}"
    }

# Function to make API request with retry logic
def make_api_request_with_retry(url, headers, params=None, max_retries=3, return_on_error=False):
    """
    Make API request with retry logic and rate limit handling.
    
    Args:
        url: API URL
        headers: Request headers
        params: Query parameters
        max_retries: Maximum number of retry attempts
        return_on_error: If True, return response even on non-200 status (for special handling)
    
    Returns:
        Response object on success, or None on failure
    """
    logger.info(f"[API REQUEST] URL: {url}")
    if params:
        logger.info(f"[API REQUEST] Params: {params}")
    retry_delay = 1
    
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            
            if response.status_code == 200:
                return response
            elif response.status_code == 403:
                # Rate limit exceeded
                if 'X-RateLimit-Reset' in response.headers:
                    reset_time = int(response.headers['X-RateLimit-Reset'])
                    wait_time = reset_time - int(time.time()) + 10
                    logger.warning(f"Rate limit exceeded. Waiting {wait_time} seconds...")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.warning(f"Rate limit exceeded")
                    return None
            elif response.status_code == 400:
                # Bad Request - don't retry, this is a client error
                if return_on_error:
                    return response
                else:
                    logger.warning(f"Request failed: {response.status_code} - {response.text}")
                    return None
            else:
                # Return response for special error handling if requested
                if return_on_error and attempt == max_retries - 1:
                    return response
                
                logger.warning(f"Request failed: {response.status_code} - {response.text}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay * (2 ** attempt))
                    continue
                return None
                
        except requests.exceptions.RequestException as e:
            logger.warning(f"Request failed (attempt {attempt + 1}): {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay * (2 ** attempt))
                continue
            return None
    
    return None

# Function to map field names to field IDs using Jira field API
def process_extended_fields(field_names, api_url, headers):
    """
    Get the field_id from the field names displayed in jira.
    Maps field names to field IDs using the /rest/api/3/field endpoint.

    Args:
        field_names: List of field names to map
        api_url: Jira API URL
        headers: Authentication headers

    Returns:
        List of tuples (field_name, field_id)
    """
    if not field_names:
        return []

    processed_fields = []
    try:
        url = f"{api_url}/rest/api/3/field"
        response = make_api_request_with_retry(url, headers)

        if response:
            response_fields = response.json()
            for response_field in response_fields:
                if response_field.get('name') in field_names:
                    field_id = response_field.get('id')
                    field_name = response_field.get('name')
                    processed_fields.append((field_name, field_id))
                    logger.info(f"Field ID for '{field_name}': {field_id}")

    except Exception as e:
        logger.error(f"Error fetching field mappings: {str(e)}")

    return processed_fields

# Function to fetch issues from Jira API
def fetch_jira_issues(api_url, project, start_date, end_date, headers, extended_attribute_ids, process_page_callback=None):
    """
    Fetch issues from Jira API using JQL query.
    If process_page_callback is provided, it will be called for each page of results.
    """
    # Build JQL query
    jql_parts = [f'project="{project}"']
    if start_date:
        start_date_str = start_date.strftime('%Y-%m-%d %H:%M')
        jql_parts.append(f'updated > "{start_date_str}"')
    if end_date:
        end_date_str = end_date.strftime('%Y-%m-%d %H:%M')
        jql_parts.append(f'updated <= "{end_date_str}"')

    # Add sorting by updated date (newest first) for better performance
    jql = ' AND '.join(jql_parts) + ' ORDER BY updated DESC'

    # Build fields to fetch
    fields = "issuetype,project,summary,status,created,updated,parent,labels,assignee"
    if extended_attribute_ids:
        fields += f",{','.join([f[1] for f in extended_attribute_ids])}"

    # Fetch issues with pagination
    all_issues = []
    next_page_token = None
    max_results = 100
    total_inserted = 0
    total_duplicates = 0

    while True:
        params = {
            "jql": jql,
            "maxResults": max_results,
            "fields": fields,
        }

        # Add nextPageToken if available
        if next_page_token:
            params["nextPageToken"] = next_page_token

        url = f"{api_url}/rest/api/3/search/jql"
        response = make_api_request_with_retry(url, headers, params)

        if response:
            data = response.json()
            # Check if data is a dict with expected structure
            if isinstance(data, dict) and 'issues' in data:
                issues = data.get("issues", [])
                if not issues:
                    break
                # Extract nextPageToken for pagination
                next_page_token = data.get("nextPageToken")
            else:
                logger.warning(f"Unexpected response format for issues: {data}")
                break
        else:
            break

        # Check for early termination (since we're sorted by updated DESC)
        if issues and start_date:
            first_issue_updated = issues[0].get('fields', {}).get('updated')
            if first_issue_updated:
                try:
                    first_issue_datetime = datetime.strptime(first_issue_updated, "%Y-%m-%dT%H:%M:%S.%f%z")
                    if first_issue_datetime.date() < start_date.date():
                        logger.info(f"Reached issues older than {start_date.date()}, stopping processing")
                        break
                except ValueError:
                    pass  # Continue if date parsing fails

        # Process this page
        if process_page_callback:
            # Per-page processing
            page_inserted, page_duplicates = process_page_callback(issues)
            total_inserted += page_inserted
            total_duplicates += page_duplicates

            # Log progress every 10 pages (approximate since we don't have page numbers with nextPageToken)
            if total_inserted % 1000 == 0 and total_inserted > 0:
                logger.info(f"Processed {total_inserted} inserted issues so far ({total_duplicates} duplicates)")
        else:
            # Legacy behavior - collect all issues
            all_issues.extend(issues)

        # Check if we have a next page token
        if not next_page_token:
            break

        if not process_page_callback:
            logger.info(f"Fetched {len(all_issues)} issues so far...")

    if process_page_callback:
        return total_inserted, total_duplicates
    else:
        return all_issues

# Function to process issues per page
def process_issues_page(issues, last_modified_date, api_url, headers, save_output_fn, extended_attribute_ids=None):
    """Process a page of issues and save them using the provided save function."""
    all_events = []

    for issue in issues:
        issue_key = issue["key"]

        try:
            # Get changelog for this issue
            changelog = get_issue_changelog(api_url, issue_key, headers)

            # Process events for this issue
            events = process_issue_events(issue, changelog, last_modified_date, None, extended_attribute_ids or [])
            all_events.extend(events)
        except Exception as e:
            logger.error(f"Error processing issue {issue_key}: {str(e)}")
            continue

    # Save this page's events using the provided save function
    if all_events:
        return save_output_fn(all_events)

    return 0, 0

# Function to get issue changelog
def get_issue_changelog(api_url, issue_key, headers):
    """Fetch the changelog for a specific issue."""
    try:
        url = f"{api_url}/rest/api/3/issue/{issue_key}/changelog"
        response = make_api_request_with_retry(url, headers)

        if response:
            data = response.json()
            # Check if data is a dict with expected structure
            if isinstance(data, dict) and 'values' in data:
                return data.get("values", [])
            else:
                logging.warning(f"Unexpected response format for changelog in {issue_key}: {data}")
                return []
    except Exception as ex:
        logger.error(f"Error fetching changelog for {issue_key}: {ex}")
        return []

    return []

# Function to process issue events
def process_issue_events(issue, changelog, start_date, end_date, extended_attribute_ids):
    """
    Process issue events from changelog and return list of events.
    Sprint information is tracked from changelog to reflect the sprint at each point in time.
    """
    events = []
    if not issue or not changelog:
        return events

    issue_fields = issue["fields"]
    issue_key = issue["key"]

    # Get base event data
    created_date_str = issue_fields.get("created")
    if created_date_str:
        created_date = Utils.convert_to_utc(created_date_str)
        if created_date is None:
            return events
    else:
        return events

    # Build extended attributes once (without sprint - will be added from changelog)
    base_extended_attributes = build_extended_attributes(issue_fields, extended_attribute_ids)
    
    # Parse to dict to allow updates
    extended_attrs_dict = json.loads(base_extended_attributes) if base_extended_attributes else {}

    # Get parent work item ID
    parent_work_item_id = issue_fields.get("parent", {}).get("key") if "parent" in issue_fields else None

    # Base event data with extended_attributes
    base_event = {
        "work_item_id": issue_key,
        "parent_work_item_id": parent_work_item_id,
        "type": issue_fields.get("issuetype", {}).get("name", ""),
        "project": issue_fields.get("project", {}).get("key", ""),
        "title": issue_fields.get("summary", ""),
        "extended_attributes": json.dumps(extended_attrs_dict) if extended_attrs_dict else None
    }

    # If start_date is set (incremental extraction), reconstruct sprint state up to start_date
    # by processing all sprint changes that occurred before start_date
    if start_date:
        for history in changelog:
            hist_created = history.get("created")
            if not hist_created:
                continue
            
            hist_timestamp = Utils.convert_to_utc(hist_created)
            if hist_timestamp is None:
                continue
            
            # Only process changes up to and including start_date nd Check for sprint changes and update base_event
            if hist_timestamp <= start_date:
                sprint_item = next((item for item in history.get("items", []) if item.get("field") == "Sprint"), None)
                if sprint_item:
                    sprint_id_str = sprint_item.get("to", "")
                    sprint_name_str = sprint_item.get("toString", "")
                    
                    if sprint_id_str:
                        try:
                            extended_attrs_dict['sprint_id'] = int(sprint_id_str.split(',')[-1].strip())
                        except (ValueError, AttributeError):
                            pass
                    if sprint_name_str:
                        extended_attrs_dict['sprint'] = sprint_name_str.split(',')[-1].strip()
                    
                    # Update base_event
                    base_event["extended_attributes"] = json.dumps(extended_attrs_dict) if extended_attrs_dict else None
            else:
                break

    # Process initial creation event
    if not start_date or created_date > start_date:
        if not end_date or created_date <= end_date:
            # Get initial status
            initial_status = issue_fields.get("status", {}).get("name", "")

            events.append({
                **base_event,
                "timestamp_utc": created_date,
                "state": initial_status,
                "event": "Work Item Created",
                "description_revisions": 0
            })

    # Process changelog events
    for history in changelog:
        hist_created = history.get("created")
        if not hist_created:
            continue

        hist_timestamp = Utils.convert_to_utc(hist_created)
        if hist_timestamp is None:
            continue

        # Check if this history entry is within our date range
        if start_date and hist_timestamp <= start_date:
            continue
        if end_date and hist_timestamp > end_date:
            continue

        # Check for sprint changes and update base_event
        sprint_item = next((item for item in history.get("items", []) if item.get("field") == "Sprint"), None)
        if sprint_item:
            sprint_id_str = sprint_item.get("to", "")
            sprint_name_str = sprint_item.get("toString", "")
            
            if sprint_id_str:
                try:
                    extended_attrs_dict['sprint_id'] = int(sprint_id_str.split(',')[-1].strip())
                except (ValueError, AttributeError):
                    pass
            if sprint_name_str:
                extended_attrs_dict['sprint'] = sprint_name_str.split(',')[-1].strip()
            
            base_event["extended_attributes"] = json.dumps(extended_attrs_dict) if extended_attrs_dict else None

        # Check for content changes
        description_revisions = has_content_changed(history.get("items", []))

        # Check for status changes (highest priority)
        status_item = next((item for item in history.get("items", []) if item.get("field") == "status"), None)
        if status_item:
            events.append({
                **base_event,
                "timestamp_utc": hist_timestamp,
                "state": status_item.get("toString", ""),
                "event": "Work Item State Changed",
                "description_revisions": description_revisions
            })
        # Check for sprint changes (second priority)
        elif sprint_item:
            current_status = issue_fields.get("status", {}).get("name", "")
            events.append({
                **base_event,
                "timestamp_utc": hist_timestamp,
                "state": current_status,
                "event": "Work Item Sprint Changed",
                "description_revisions": description_revisions
            })
        else:
            # Generic update event
            current_status = issue_fields.get("status", {}).get("name", "")
            events.append({
                **base_event,
                "timestamp_utc": hist_timestamp,
                "state": current_status,
                "event": "Work Item Updated",
                "description_revisions": description_revisions
            })

    return events


def get_project_key_by_name(api_url, project_name, headers):
    """
    Get project key by searching for project name.
    
    Args:
        api_url: Jira API base URL
        project_name: Project name to search for
        headers: Authentication headers
        
    Returns:
        Project key if found, None otherwise
    """
    try:
        url = f"{api_url}/rest/api/3/project/search"
        params = {"query": project_name}
        
        response = make_api_request_with_retry(url, headers, params)
        
        if response:
            data = response.json()
            if isinstance(data, dict) and 'values' in data:
                projects = data.get('values', [])
                # Look for exact match on name
                for proj in projects:
                    if proj.get('name', '').lower() == project_name.lower():
                        project_key = proj.get('key')
                        logger.info(f"Found project key '{project_key}' for project name '{project_name}'")
                        return project_key
                # If no exact match, try first result
                if projects:
                    project_key = projects[0].get('key')
                    logger.info(f"Using project key '{project_key}' for project name '{project_name}' (fuzzy match)")
                    return project_key
    except Exception as e:
        logger.error(f"Error looking up project key for '{project_name}': {e}")
    
    return None


def fetch_jira_boards(api_url, project, headers):
    """
    Fetch all boards for a given project.
    
    Args:
        api_url: Jira API base URL
        project: Project key or project name
        headers: Authentication headers
        
    Returns:
        List of board dictionaries
    """
    all_boards = []
    start_at = 0
    max_results = 50

    # First, try to fetch boards with the project as-is
    project_key = project
    project_key_resolved = False

    while True:
        url = f"{api_url}/rest/agile/1.0/board"
        params = {
            "startAt": start_at,
            "maxResults": max_results
        }
        
        # Add projectKeyOrId parameter
        if project_key:
            params["projectKeyOrId"] = project_key

        response = make_api_request_with_retry(url, headers, params, return_on_error=True)
        
        if response is None:
            logger.warning(f"No response received for board request")
            return all_boards
        
        status_code = getattr(response, 'status_code', None)

        if status_code == 200:
            data = response.json()
            if isinstance(data, dict) and 'values' in data:
                boards = data.get("values", [])
                if not boards:
                    # No boards found, return what we have
                    return all_boards
                
                all_boards.extend(boards)
                
                # Check if there are more results
                is_last = data.get("isLast", True)
                if is_last:
                    return all_boards
                
                start_at += max_results
            else:
                logger.warning(f"Unexpected response format for boards: {data}")
                return all_boards

        elif status_code == 400:
            # Project not found - try to resolve project name to key
            try:
                response_data = response.json() if response.text else {}
            except Exception as e:
                logger.error(f"Failed to parse error response: {e}")
                return all_boards
                
            error_messages = response_data.get("errorMessages", [])
            
            if any("No project could be found" in msg for msg in error_messages):
                if not project_key_resolved:
                    # Try to get project key by name
                    logger.info(f"Project '{project}' not found by key, attempting to resolve project name to key...")
                    resolved_key = get_project_key_by_name(api_url, project, headers)
                    
                    if resolved_key and resolved_key != project_key:
                        logger.info(f"Resolved project key: '{resolved_key}' for project: '{project}'")
                        project_key = resolved_key
                        project_key_resolved = True
                        start_at = 0  # Reset pagination
                        continue
                    else:
                        logger.warning(f"Could not resolve project name '{project}' to a valid project key")
                
                # If we still can't find it, return empty
                logger.warning(f"Could not find boards for project '{project}'")
                return all_boards
            else:
                logger.warning(f"Failed to fetch boards: {response.status_code} - {response.text}")
                return all_boards
        else:
            logger.warning(f"Unexpected status code {status_code} when fetching boards")
            return all_boards

    return all_boards


def fetch_board_sprints(api_url, board_id, headers):
    """
    Fetch all sprints for a given board.
    
    Args:
        api_url: Jira API base URL
        board_id: Board ID
        headers: Authentication headers
        
    Returns:
        List of sprint dictionaries
    """
    all_sprints = []
    start_at = 0
    max_results = 50

    while True:
        url = f"{api_url}/rest/agile/1.0/board/{board_id}/sprint"
        params = {
            "startAt": start_at,
            "maxResults": max_results
        }

        response = make_api_request_with_retry(url, headers, params)

        if response:
            data = response.json()
            if isinstance(data, dict) and 'values' in data:
                sprints = data.get("values", [])
                if not sprints:
                    return all_sprints
                
                all_sprints.extend(sprints)
                
                # Check if there are more results
                is_last = data.get("isLast", True)
                if is_last:
                    return all_sprints
                
                start_at += max_results
            else:
                logger.warning(f"Unexpected response format for sprints: {data}")
                return all_sprints
        else:
            return all_sprints

    return all_sprints


def process_sprints(sprints: list, board_name: str, project: str) -> list:
    """
    Process sprint data from Jira API.
    
    Args:
        sprints: List of sprint dictionaries from Jira API
        board_name: Name of the board
        project: Project key
        
    Returns:
        List of sprint dictionaries
    """
    sprint_data = []

    for sprint in sprints:
        sprint_name = sprint.get('name')
        sprint_id = sprint.get('id')
        
        if not sprint_name or not sprint_id:
            continue

        # Get sprint status (state in Jira)
        status = sprint.get('state', '')

        # Parse dates from Jira API
        created_date = sprint.get('createdDate')
        start_date = sprint.get('startDate')
        end_date = sprint.get('endDate')
        completion_date = sprint.get('completeDate')

        # Convert dates to UTC datetime
        created_date_utc = Utils.convert_to_utc(created_date) if created_date else None
        start_date_utc = Utils.convert_to_utc(start_date) if start_date else None
        end_date_utc = Utils.convert_to_utc(end_date) if end_date else None
        completion_date_utc = Utils.convert_to_utc(completion_date) if completion_date else None

        sprint_data.append({
            "sprint_id": sprint_id,
            "sprint_name": sprint_name,
            "project": project,
            "board_name": board_name,
            "created_date_utc": created_date_utc,
            "start_date_utc": start_date_utc,
            "end_date_utc": end_date_utc,
            "completion_date_utc": completion_date_utc,
            "status": status
        })

    return sprint_data


# Main Execution: Fetch work items modified since the last known date
def main():
    parser = argparse.ArgumentParser(description="Extract Jira work item events to database or CSV.")

    # Add command-line arguments
    parser.add_argument('-p', '--product', type=str, help='Product name (if provided, saves to database; otherwise saves to CSV)')
    parser.add_argument('-s', '--start-date', type=str, help='Start date in YYYY-MM-DD format')

    # Parse the arguments
    args = parser.parse_args()

    extractor = JiraExtractor()

    if args.product is None:
        # CSV Mode: Load configuration from config.json
        config = json.load(open(os.path.join(common_dir, "config.json")))

        # Build config dictionary with defaults and parsing
        jira_config = {
            'jira_api_url': f"https://{config.get('JIRA_ORGANIZATION')}.atlassian.net",
            'jira_user_email': config.get('JIRA_USER_EMAIL'),
            'jira_api_token': config.get('JIRA_API_TOKEN'),
            'projects': config.get('JIRA_PROJECTS', '').split(',') if config.get('JIRA_PROJECTS') else []
        }

        # Get custom fields from config
        jira_custom_fields_str = config.get('JIRA_WORK_ITEM_ATTRIBUTES', '')
        if jira_custom_fields_str:
            try:
                # Parse comma-separated custom fields
                custom_fields = [field.strip() for field in jira_custom_fields_str.split(',') if field.strip()]
                jira_config['jira_custom_fields'] = custom_fields
            except Exception as e:
                logger.warning(f"Failed to parse JIRA_WORK_ITEM_ATTRIBUTES: {e}")
                jira_config['jira_custom_fields'] = []
        else:
            jira_config['jira_custom_fields'] = []

        config = jira_config

        # Use checkpoint file for last modified date
        checkpoint_file = "jira"
        last_modified = Utils.load_checkpoint(checkpoint_file)

        extractor.run_extraction(None, config, args.start_date, last_modified)

    else:
        # Database Mode: Connect to the database
        from database import DatabaseConnection
        db = DatabaseConnection()

        with db.product_scope(args.product) as conn:
            with conn.cursor() as cursor:
                config = extractor.get_config_from_database(cursor)
                last_modified = extractor.get_last_modified_date(cursor)
                extractor.run_extraction(cursor, config, args.start_date, last_modified)

if __name__ == '__main__':
    exit_code = main()
    sys.exit(exit_code)
