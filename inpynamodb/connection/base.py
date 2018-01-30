"""
Lowest level connection
"""
import asyncio
import logging

import math
import warnings
from base64 import b64decode

import six
import time
from aiobotocore import get_session
from botocore.exceptions import BotoCoreError, ClientError
from pynamodb.compat import NullHandler

from pynamodb.connection.util import pythonic
from inpynamodb.constants import SERVICE_NAME, TABLE_NAME, ITEM, CONDITION_EXPRESSION, EXPRESSION_ATTRIBUTE_NAMES, \
    EXPRESSION_ATTRIBUTE_VALUES, PUT_ITEM, AND, DESCRIBE_TABLE, LIST_TABLES, UPDATE_TABLE, DELETE_TABLE, CREATE_TABLE, \
    RETURN_CONSUMED_CAPACITY, TOTAL, CONSUMED_CAPACITY, CAPACITY_UNITS, PROVISIONED_THROUGHPUT, READ_CAPACITY_UNITS, \
    WRITE_CAPACITY_UNITS, ATTR_NAME, ATTR_TYPE, ATTR_DEFINITIONS, INDEX_NAME, KEY_SCHEMA, PROJECTION, KEY_TYPE, \
    GLOBAL_SECONDARY_INDEXES, LOCAL_SECONDARY_INDEXES, STREAM_SPECIFICATION, STREAM_ENABLED, STREAM_VIEW_TYPE, \
    TABLE_KEY, KEY, COMPARISON_OPERATOR, COMPARISON_OPERATOR_VALUES, KEY_CONDITION_OPERATOR_MAP, ATTR_VALUE_LIST, \
    KEY_CONDITION_EXPRESSION, FILTER_EXPRESSION, PROJECTION_EXPRESSION, CONSISTENT_READ, LIMIT, SELECT_VALUES, SELECT, \
    SCAN_INDEX_FORWARD, QUERY, ATTR_UPDATES, ACTION, ATTR_UPDATE_ACTIONS, VALUE, DELETE, UPDATE_EXPRESSION, PUT, \
    UPDATE_ITEM, DELETE_ITEM, SEGMENT, TOTAL_SEGMENTS, SCAN, DELETE_REQUEST, REQUEST_ITEMS, BATCH_WRITE_ITEM, \
    PUT_REQUEST, KEYS, BATCH_GET_ITEM, EXCLUSIVE_START_TABLE_NAME, UPDATE, GLOBAL_SECONDARY_INDEX_UPDATES, EXISTS, \
    NOT_NULL, NULL, EQ, QUERY_FILTER_VALUES, NOT_CONTAINS, FILTER_EXPRESSION_OPERATOR_MAP, CONDITIONAL_OPERATOR, \
    GET_ITEM, SHORT_ATTR_TYPES, EXCLUSIVE_START_KEY, ITEMS, LAST_EVALUATED_KEY, CONDITIONAL_OPERATORS, EXPECTED, \
    QUERY_FILTER, RETURN_CONSUMED_CAPACITY_VALUES, RETURN_ITEM_COLL_METRICS, RETURN_VALUES_VALUES, RETURN_VALUES, \
    RETURN_ITEM_COLL_METRICS_VALUES, BINARY_SHORT, DEFAULT_ENCODING, BINARY_SET_SHORT, RESPONSES, UNPROCESSED_KEYS, \
    UNPROCESSED_ITEMS
from pynamodb.exceptions import PutError, TableError, TableDoesNotExist, QueryError, UpdateError, DeleteError, \
    ScanError, GetError

from inpynamodb.expressions.condition import Condition
from inpynamodb.expressions.operand import Path
from inpynamodb.expressions.projection import create_projection_expression
from inpynamodb.expressions.update import Update
from pynamodb.types import RANGE, HASH

from inpynamodb.settings import get_settings_value

BOTOCORE_EXCEPTIONS = (BotoCoreError, ClientError)

log = logging.getLogger(__name__)
log.addHandler(NullHandler())


class MetaTable(object):
    """
    A pythonic wrapper around table metadata
    """

    def __init__(self, data):
        self.data = data or {}
        self._range_keyname = None
        self._hash_keyname = None

    def __repr__(self):
        if self.data:
            return six.u("MetaTable<{0}>".format(self.data.get(TABLE_NAME)))

    @property
    def range_keyname(self):
        """
        Returns the name of this table's range key
        """
        if self._range_keyname is None:
            for attr in self.data.get(KEY_SCHEMA):
                if attr.get(KEY_TYPE) == RANGE:
                    self._range_keyname = attr.get(ATTR_NAME)
        return self._range_keyname

    @property
    def hash_keyname(self):
        """
        Returns the name of this table's hash key
        """
        if self._hash_keyname is None:
            for attr in self.data.get(KEY_SCHEMA):
                if attr.get(KEY_TYPE) == HASH:
                    self._hash_keyname = attr.get(ATTR_NAME)
                    break
        return self._hash_keyname

    def get_index_hash_keyname(self, index_name):
        """
        Returns the name of the hash key for a given index
        """
        global_indexes = self.data.get(GLOBAL_SECONDARY_INDEXES)
        local_indexes = self.data.get(LOCAL_SECONDARY_INDEXES)
        indexes = []
        if local_indexes:
            indexes += local_indexes
        if global_indexes:
            indexes += global_indexes
        for index in indexes:
            if index.get(INDEX_NAME) == index_name:
                for schema_key in index.get(KEY_SCHEMA):
                    if schema_key.get(KEY_TYPE) == HASH:
                        return schema_key.get(ATTR_NAME)

    def get_index_range_keyname(self, index_name):
        """
        Returns the name of the hash key for a given index
        """
        global_indexes = self.data.get(GLOBAL_SECONDARY_INDEXES)
        local_indexes = self.data.get(LOCAL_SECONDARY_INDEXES)
        indexes = []
        if local_indexes:
            indexes += local_indexes
        if global_indexes:
            indexes += global_indexes
        for index in indexes:
            if index.get(INDEX_NAME) == index_name:
                for schema_key in index.get(KEY_SCHEMA):
                    if schema_key.get(KEY_TYPE) == RANGE:
                        return schema_key.get(ATTR_NAME)
        return None

    def get_item_attribute_map(self, attributes, item_key=ITEM, pythonic_key=True):
        """
        Builds up a dynamodb compatible AttributeValue map
        """
        if pythonic_key:
            item_key = item_key
        attr_map = {
            item_key: {}
        }
        for key, value in attributes.items():
            # In this case, the user provided a mapping
            # {'key': {'S': 'value'}}
            if isinstance(value, dict):
                attr_map[item_key][key] = value
            else:
                attr_map[item_key][key] = {
                    self.get_attribute_type(key): value
                }
        return attr_map

    def get_attribute_type(self, attribute_name, value=None):
        """
        Returns the proper attribute type for a given attribute name
        """
        for attr in self.data.get(ATTR_DEFINITIONS):
            if attr.get(ATTR_NAME) == attribute_name:
                return attr.get(ATTR_TYPE)
        if value is not None and isinstance(value, dict):
            for key in SHORT_ATTR_TYPES:
                if key in value:
                    return key
        attr_names = [attr.get(ATTR_NAME) for attr in self.data.get(ATTR_DEFINITIONS)]
        raise ValueError("No attribute {0} in {1}".format(attribute_name, attr_names))

    def get_identifier_map(self, hash_key, range_key=None, key=KEY):
        """
        Builds the identifier map that is common to several operations
        """
        kwargs = {
            key: {
                self.hash_keyname: {
                    self.get_attribute_type(self.hash_keyname): hash_key
                }
            }
        }
        if range_key is not None:
            kwargs[key][self.range_keyname] = {
                self.get_attribute_type(self.range_keyname): range_key
            }
        return kwargs

    def get_exclusive_start_key_map(self, exclusive_start_key):
        """
        Builds the exclusive start key attribute map
        """
        if isinstance(exclusive_start_key, dict) and self.hash_keyname in exclusive_start_key:
            # This is useful when paginating results, as the LastEvaluatedKey returned is already
            # structured properly
            return {
                EXCLUSIVE_START_KEY: exclusive_start_key
            }
        else:
            return {
                EXCLUSIVE_START_KEY: {
                    self.hash_keyname: {
                        self.get_attribute_type(self.hash_keyname): exclusive_start_key
                    }
                }
            }


class AsyncConnection(object):
    """
    A higher level abstraction over aiobotocore
    """
    def __init__(self, region=None, host=None, session_cls=None, request_timeout_seconds=None, max_retry_attempts=None,
                 base_backoff_ms=None):
        self._tables = {}
        self.host = host
        self._client = None
        self._session = None
        self._requests_session = None
        if region:
            self.region = region
        else:
            self.region = get_settings_value('region')

        if session_cls:
            self.session_cls = session_cls
        else:
            self.session_cls = get_settings_value('session_cls')

        if request_timeout_seconds is not None:
            self._request_timeout_seconds = request_timeout_seconds
        else:
            self._request_timeout_seconds = get_settings_value('request_timeout_seconds')

        if max_retry_attempts is not None:
            self._max_retry_attempts_exception = max_retry_attempts
        else:
            self._max_retry_attempts_exception = get_settings_value('max_retry_attempts')

        if base_backoff_ms is not None:
            self._base_backoff_ms = base_backoff_ms
        else:
            self._base_backoff_ms = get_settings_value('base_backoff_ms')

    def __repr__(self):
        return six.u("Connection<{0}>".format(self.client.meta.endpoint_url))

    def __del__(self):
        self.close_session()

    @property
    def session(self):
        """
        Returns a valid aiobotocore session
        """
        if self._session is None:
            self._session = get_session()
        return self._session

    def _log_debug(self, operation, kwargs):
        """
        Sends a debug message to the logger
        """
        log.debug("Calling %s with arguments %s", operation, kwargs)

    def _log_debug_response(self, operation, response):
        """
        Sends a debug message to the logger about a response
        """
        log.debug("%s response: %s", operation, response)

    def _log_error(self, operation, response):
        """
        Sends an error message to the logger
        """
        log.error("%s failed with status: %s, message: %s",
                  operation, response.status_code,response.content)

    def close_session(self):
        self.client.close()

    @property
    def requests_session(self):
        """
        Return a requests session to execute prepared requests using the same pool
        """
        if self._requests_session is None:
            self._requests_session = self.session_cls()
        return self._requests_session

    async def _get_filter_expression(self, table_name, filters, conditional_operator,
                                     name_placeholders, expression_attribute_values):
        """
        Builds the FilterExpression needed for Query and Scan operations
        """
        condition_expression = None
        conditional_operator = conditional_operator[CONDITIONAL_OPERATOR]
        # We sort the keys here for determinism. This is mostly done to simplify testing.
        for key in sorted(filters.keys()):
            condition = filters[key]
            operator = condition.get(COMPARISON_OPERATOR)
            if operator not in QUERY_FILTER_VALUES:
                raise ValueError("{0} must be one of {1}".format(COMPARISON_OPERATOR, QUERY_FILTER_VALUES))
            not_contains = operator == NOT_CONTAINS
            operator = FILTER_EXPRESSION_OPERATOR_MAP[operator]
            values = condition.get(ATTR_VALUE_LIST, [])
            condition = await self._get_condition(table_name, key, operator, *values)
            if not_contains:
                condition = ~condition
            if condition_expression is None:
                condition_expression = condition
            elif conditional_operator == AND:
                condition_expression = condition_expression & condition
            else:
                condition_expression = condition_expression | condition
        return condition_expression.serialize(name_placeholders, expression_attribute_values)

    async def _get_condition(self, table_name, attribute_name, operator, *values):
        values = [
            {await self.get_attribute_type(table_name, attribute_name, value): self.parse_attribute(value)}
            for value in values
        ]
        return getattr(Path([attribute_name]), operator)(*values)

    async def get_attribute_type(self, table_name, attribute_name, value=None):
        """
        Returns the proper attribute type for a given attribute name
        :param value: The attribute value an be supplied just in case the type is already included
        """
        tbl = await self.get_meta_table(table_name)
        if tbl is None:
            raise TableError("No such table {0}".format(table_name))
        return tbl.get_attribute_type(attribute_name, value=value)

    async def query(self,
                    table_name,
                    hash_key,
                    range_key_condition=None,
                    filter_condition=None,
                    attributes_to_get=None,
                    consistent_read=False,
                    exclusive_start_key=None,
                    index_name=None,
                    key_conditions=None,
                    query_filters=None,
                    conditional_operator=None,
                    limit=None,
                    return_consumed_capacity=None,
                    scan_index_forward=None,
                    select=None):
        """
        Performs the Query operation and returns the result
        """
        self._check_condition('range_key_condition', range_key_condition, key_conditions, conditional_operator)
        self._check_condition('filter_condition', filter_condition, query_filters, conditional_operator)

        operation_kwargs = {TABLE_NAME: table_name}
        name_placeholders = {}
        expression_attribute_values = {}

        tbl = await self.get_meta_table(table_name)

        if tbl is None:
            raise TableError("No such table: {0}".format(table_name))
        if index_name:
            hash_keyname = tbl.get_index_hash_keyname(index_name)
            if not hash_keyname:
                raise ValueError("No hash key attribute for index: {0}".format(index_name))
            range_keyname = tbl.get_index_range_keyname(index_name)
        else:
            hash_keyname = tbl.hash_keyname
            range_keyname = tbl.range_keyname

        key_condition = await self._get_condition(table_name, hash_keyname, '__eq__', hash_key)
        if range_key_condition is not None:
            if range_key_condition.is_valid_range_key_condition(range_keyname):
                key_condition = key_condition & range_key_condition
            elif filter_condition is None:
                # Try to gracefully handle the case where a user passed in a filter as a range key condition
                (filter_condition, range_key_condition) = (range_key_condition, None)
            else:
                raise ValueError("{0} is not a valid range key condition".format(range_key_condition))

        if key_conditions is None or len(key_conditions) == 0:
            pass  # No comparisons on sort key
        elif len(key_conditions) > 1:
            raise ValueError("Multiple attributes are not supported in key_conditions: {0}".format(key_conditions))
        else:
            (key, condition), = key_conditions.items()
            operator = condition.get(COMPARISON_OPERATOR)
            if operator not in COMPARISON_OPERATOR_VALUES:
                raise ValueError("{0} must be one of {1}".format(COMPARISON_OPERATOR, COMPARISON_OPERATOR_VALUES))
            operator = KEY_CONDITION_OPERATOR_MAP[operator]
            values = condition.get(ATTR_VALUE_LIST)
            sort_key_expression = await self._get_condition(table_name, key, operator, *values)
            key_condition = key_condition & sort_key_expression

        operation_kwargs[KEY_CONDITION_EXPRESSION] = key_condition.serialize(
            name_placeholders, expression_attribute_values)
        if filter_condition is not None:
            filter_expression = filter_condition.serialize(name_placeholders, expression_attribute_values)
            # FilterExpression does not allow key attributes. Check for hash and range key name placeholders
            hash_key_placeholder = name_placeholders.get(hash_keyname)
            range_key_placeholder = range_keyname and name_placeholders.get(range_keyname)
            if (
                    hash_key_placeholder in filter_expression or
                    (range_key_placeholder and range_key_placeholder in filter_expression)
            ):
                raise ValueError("'filter_condition' cannot contain key attributes")
            operation_kwargs[FILTER_EXPRESSION] = filter_expression
        if attributes_to_get:
            projection_expression = create_projection_expression(attributes_to_get, name_placeholders)
            operation_kwargs[PROJECTION_EXPRESSION] = projection_expression
        if consistent_read:
            operation_kwargs[CONSISTENT_READ] = True
        if exclusive_start_key:
            operation_kwargs.update(await self.get_exclusive_start_key_map(table_name, exclusive_start_key))
        if index_name:
            operation_kwargs[INDEX_NAME] = index_name
        if limit is not None:
            operation_kwargs[LIMIT] = limit
        if return_consumed_capacity:
            operation_kwargs.update(self.get_consumed_capacity_map(return_consumed_capacity))
        # We read the conditional operator even without a query filter passed in to maintain existing behavior.
        conditional_operator = self.get_conditional_operator(conditional_operator or AND)
        if query_filters:
            filter_expression = self._get_filter_expression(
                table_name, query_filters, conditional_operator, name_placeholders, expression_attribute_values)
            operation_kwargs[FILTER_EXPRESSION] = filter_expression
        if select:
            if select.upper() not in SELECT_VALUES:
                raise ValueError("{0} must be one of {1}".format(SELECT, SELECT_VALUES))
            operation_kwargs[SELECT] = str(select).upper()
        if scan_index_forward is not None:
            operation_kwargs[SCAN_INDEX_FORWARD] = scan_index_forward
        if name_placeholders:
            operation_kwargs[EXPRESSION_ATTRIBUTE_NAMES] = self._reverse_dict(name_placeholders)
        if expression_attribute_values:
            operation_kwargs[EXPRESSION_ATTRIBUTE_VALUES] = expression_attribute_values

        try:
            return await self.dispatch(QUERY, operation_kwargs)
        except BOTOCORE_EXCEPTIONS as e:
            raise QueryError("Failed to query items: {0}".format(e), e)

    def parse_attribute(self, attribute, return_type=False):
        """
        Returns the attribute value, where the attribute can be
        a raw attribute value, or a dictionary containing the type:
        {'S': 'String value'}
        """
        if isinstance(attribute, dict):
            for key in SHORT_ATTR_TYPES:
                if key in attribute:
                    if return_type:
                        return key, attribute.get(key)
                    return attribute.get(key)
            raise ValueError("Invalid attribute supplied: {0}".format(attribute))
        else:
            if return_type:
                return None, attribute
            return attribute

    async def _get_condition_expression(self, table_name, expected, conditional_operator,
                                        name_placeholders, expression_attribute_values):
        """
        Builds the ConditionExpression needed for DeleteItem, PutItem, and UpdateItem operations
        """
        condition_expression = None
        conditional_operator = conditional_operator[CONDITIONAL_OPERATOR]
        # We sort the keys here for determinism. This is mostly done to simplify testing.
        for key in sorted(expected.keys()):
            condition = expected[key]
            if EXISTS in condition:
                operator = NOT_NULL if condition.get(EXISTS, True) else NULL
                values = []
            elif VALUE in condition:
                operator = EQ
                values = [condition.get(VALUE)]
            else:
                operator = condition.get(COMPARISON_OPERATOR)
                values = condition.get(ATTR_VALUE_LIST, [])
            if operator not in QUERY_FILTER_VALUES:
                raise ValueError("{0} must be one of {1}".format(COMPARISON_OPERATOR, QUERY_FILTER_VALUES))
            not_contains = operator == NOT_CONTAINS
            operator = FILTER_EXPRESSION_OPERATOR_MAP[operator]
            condition = await self._get_condition(table_name, key, operator, *values)
            if not_contains:
                condition = ~condition
            if condition_expression is None:
                condition_expression = condition
            elif conditional_operator == AND:
                condition_expression = condition_expression & condition
            else:
                condition_expression = condition_expression | condition
        return condition_expression.serialize(name_placeholders, expression_attribute_values)

    async def get_exclusive_start_key_map(self, table_name, exclusive_start_key):
        """
        Builds the exclusive start key attribute map
        """
        tbl = await self.get_meta_table(table_name)
        if tbl is None:
            raise TableError("No such table {0}".format(table_name))
        return tbl.get_exclusive_start_key_map(exclusive_start_key)

    async def dispatch(self, operation_name, operation_kwargs):
        """
        Dispatches `operation_name` with arguments `operation_kwargs`

        Raises TableDoesNotExist if the specified table does not exist
        """
        if operation_name not in [DESCRIBE_TABLE, LIST_TABLES, UPDATE_TABLE, DELETE_TABLE, CREATE_TABLE]:
            if RETURN_CONSUMED_CAPACITY not in operation_kwargs:
                operation_kwargs.update(self.get_consumed_capacity_map(TOTAL))
        self._log_debug(operation_name, operation_kwargs)

        # TODO signals will be implemented.
        # self.send_pre_boto_callback(operation_name, req_uuid, table_name)
        # self.send_post_boto_callback(operation_name, req_uuid, table_name)\
        data = await self.client._make_api_call(operation_name, operation_kwargs)

        if data and CONSUMED_CAPACITY in data:
            capacity = data.get(CONSUMED_CAPACITY)
            if isinstance(capacity, dict) and CAPACITY_UNITS in capacity:
                capacity = capacity.get(CAPACITY_UNITS)
            log.debug("%s %s consumed %s units", data.get(TABLE_NAME, ''), operation_name, capacity)
        return data

    async def batch_write_item(self,
                               table_name,
                               put_items=None,
                               delete_items=None,
                               return_consumed_capacity=None,
                               return_item_collection_metrics=None):
        """
        Performs the batch_write_item operation
        """
        if put_items is None and delete_items is None:
            raise ValueError("Either put_items or delete_items must be specified")
        operation_kwargs = {
            REQUEST_ITEMS: {
                table_name: []
            }
        }
        if return_consumed_capacity:
            operation_kwargs.update(self.get_consumed_capacity_map(return_consumed_capacity))
        if return_item_collection_metrics:
            operation_kwargs.update(self.get_item_collection_map(return_item_collection_metrics))
        put_items_list = []
        if put_items:
            for item in put_items:
                put_items_list.append({
                    PUT_REQUEST: await self.get_item_attribute_map(table_name, item, pythonic_key=False)
                })
        delete_items_list = []
        if delete_items:
            for item in delete_items:
                delete_items_list.append({
                    DELETE_REQUEST: await self.get_item_attribute_map(table_name, item, item_key=KEY,
                                                                      pythonic_key=False)
                })
        operation_kwargs[REQUEST_ITEMS][table_name] = delete_items_list + put_items_list
        try:
            return await self.dispatch(BATCH_WRITE_ITEM, operation_kwargs)
        except BOTOCORE_EXCEPTIONS as e:
            raise PutError("Failed to batch write items: {0}".format(e), e)

    def get_item_collection_map(self, return_item_collection_metrics):
        """
        Builds the item collection map
        """
        if return_item_collection_metrics.upper() not in RETURN_ITEM_COLL_METRICS_VALUES:
            raise ValueError("{0} must be one of {1}".format(RETURN_ITEM_COLL_METRICS, RETURN_ITEM_COLL_METRICS_VALUES))
        return {
            RETURN_ITEM_COLL_METRICS: str(return_item_collection_metrics).upper()
        }

    async def rate_limited_scan(self,
                                table_name,
                                filter_condition=None,
                                attributes_to_get=None,
                                page_size=None,
                                limit=None,
                                conditional_operator=None,
                                scan_filter=None,
                                exclusive_start_key=None,
                                segment=None,
                                total_segments=None,
                                timeout_seconds=None,
                                read_capacity_to_consume_per_second=10,
                                allow_rate_limited_scan_without_consumed_capacity=None,
                                max_sleep_between_retry=10,
                                max_consecutive_exceptions=10,
                                consistent_read=None):
        """
        Performs a rate limited scan on the table. The API uses the scan API to fetch items from
        DynamoDB. The rate_limited_scan uses the 'ConsumedCapacity' value returned from DynamoDB to
        limit the rate of the scan. 'ProvisionedThroughputExceededException' is also handled and retried.

        :param table_name: Name of the table to perform scan on.
        :param filter_condition: Condition used to restrict the scan results
        :param attributes_to_get: A list of attributes to return.
        :param page_size: Page size of the scan to DynamoDB
        :param limit: Used to limit the number of results returned
        :param conditional_operator:
        :param scan_filter: A map indicating the condition that evaluates the scan results
        :param exclusive_start_key: If set, provides the starting point for scan.
        :param segment: If set, then scans the segment
        :param total_segments: If set, then specifies total segments
        :param timeout_seconds: Timeout value for the rate_limited_scan method, to prevent it from running
            infinitely
        :param read_capacity_to_consume_per_second: Amount of read capacity to consume
            every second
        :param allow_rate_limited_scan_without_consumed_capacity: If set, proceeds without rate limiting if
            the server does not support returning consumed capacity in responses.
        :param max_sleep_between_retry: Max value for sleep in seconds in between scans during
            throttling/rate limit scenarios
        :param max_consecutive_exceptions: Max number of consecutive ProvisionedThroughputExceededException
            exception for scan to exit
        :param consistent_read: enable consistent read
        """
        read_capacity_to_consume_per_ms = float(read_capacity_to_consume_per_second) / 1000
        if allow_rate_limited_scan_without_consumed_capacity is None:
            allow_rate_limited_scan_without_consumed_capacity = get_settings_value(
                'allow_rate_limited_scan_without_consumed_capacity'
            )
        total_consumed_read_capacity = 0.0
        last_evaluated_key = exclusive_start_key
        rate_available = True
        latest_scan_consumed_capacity = 0
        consecutive_provision_throughput_exceeded_ex = 0
        start_time = time.time()

        if page_size is None:
            if limit and read_capacity_to_consume_per_second > limit:
                page_size = limit
            else:
                page_size = read_capacity_to_consume_per_second

        while True:
            if rate_available:
                try:
                    data = await self.scan(
                        table_name,
                        filter_condition=filter_condition,
                        attributes_to_get=attributes_to_get,
                        exclusive_start_key=last_evaluated_key,
                        limit=page_size,
                        conditional_operator=conditional_operator,
                        return_consumed_capacity=TOTAL,
                        scan_filter=scan_filter,
                        segment=segment,
                        total_segments=total_segments,
                        consistent_read=consistent_read
                    )
                    for item in data.get(ITEMS):
                        yield item

                        if limit is not None:
                            limit -= 1
                            if not limit:
                                return

                    if CONSUMED_CAPACITY in data:
                        latest_scan_consumed_capacity = data.get(CONSUMED_CAPACITY).get(CAPACITY_UNITS)
                    else:
                        if allow_rate_limited_scan_without_consumed_capacity:
                            latest_scan_consumed_capacity = 0
                        else:
                            raise ScanError('Rate limited scan not possible because the server did not send back'
                                            'consumed capacity information. If you wish scans to complete anyway'
                                            'without functioning rate limiting, set '
                                            'allow_rate_limited_scan_without_consumed_capacity to True in settings.')

                    last_evaluated_key = data.get(LAST_EVALUATED_KEY, None)
                    consecutive_provision_throughput_exceeded_ex = 0
                except ScanError as e:
                    # Only retry if provision throughput is exceeded.
                    if isinstance(e.cause, ClientError):
                        code = e.cause.response['Error'].get('Code')
                        if code == "ProvisionedThroughputExceededException":
                            consecutive_provision_throughput_exceeded_ex += 1
                            if consecutive_provision_throughput_exceeded_ex > max_consecutive_exceptions:
                                # Max threshold reached
                                raise
                        else:
                            # Different exception, other than ProvisionedThroughputExceededException
                            raise
                    else:
                        # Not a Client error
                        raise

            # No throttling, and no more scans needed. Just return
            if not last_evaluated_key and consecutive_provision_throughput_exceeded_ex == 0:
                return

            current_time = time.time()

            # elapsed_time_ms indicates the time taken in ms from the start of the
            # throttled_scan call.
            elapsed_time_ms = max(1, round((current_time - start_time) * 1000))

            if consecutive_provision_throughput_exceeded_ex == 0:
                total_consumed_read_capacity += latest_scan_consumed_capacity
                consumed_rate = total_consumed_read_capacity / elapsed_time_ms
                rate_available = (read_capacity_to_consume_per_ms - consumed_rate) >= 0

            # consecutive_provision_throughput_exceeded_ex > 0 indicates ProvisionedThroughputExceededException occurred.
            # ProvisionedThroughputExceededException can occur if:
            #    - The rate to consume is passed incorrectly.
            #    - External factors, even if the current scan is within limits.
            if not rate_available or (consecutive_provision_throughput_exceeded_ex > 0):
                # Minimum value is 1 second.
                elapsed_time_s = math.ceil(elapsed_time_ms / 1000)
                # Sleep proportional to the ratio of --consumed capacity-- to --capacity to consume--
                time_to_sleep = max(1, round((total_consumed_read_capacity / elapsed_time_s) \
                                             / read_capacity_to_consume_per_second))

                # At any moment if the timeout_seconds hits, then return
                if timeout_seconds and (elapsed_time_s + time_to_sleep) > timeout_seconds:
                    raise ScanError("Input timeout value {0} has expired".format(timeout_seconds))

                await asyncio.sleep(min(math.ceil(time_to_sleep), max_sleep_between_retry))
                # Reset the latest_scan_consumed_capacity, as no scan operation was performed.
                latest_scan_consumed_capacity = 0

    async def scan(self,
                   table_name,
                   filter_condition=None,
                   attributes_to_get=None,
                   limit=None,
                   conditional_operator=None,
                   scan_filter=None,
                   return_consumed_capacity=None,
                   exclusive_start_key=None,
                   segment=None,
                   total_segments=None,
                   consistent_read=None):
        """
        Performs the scan operation
        """
        self._check_condition('filter_condition', filter_condition, scan_filter, conditional_operator)

        operation_kwargs = {TABLE_NAME: table_name}
        name_placeholders = {}
        expression_attribute_values = {}

        if filter_condition is not None:
            filter_expression = filter_condition.serialize(name_placeholders, expression_attribute_values)
            operation_kwargs[FILTER_EXPRESSION] = filter_expression
        if attributes_to_get is not None:
            projection_expression = create_projection_expression(attributes_to_get, name_placeholders)
            operation_kwargs[PROJECTION_EXPRESSION] = projection_expression
        if limit is not None:
            operation_kwargs[LIMIT] = limit
        if return_consumed_capacity:
            operation_kwargs.update(self.get_consumed_capacity_map(return_consumed_capacity))
        if exclusive_start_key:
            operation_kwargs.update(await self.get_exclusive_start_key_map(table_name, exclusive_start_key))
        if segment is not None:
            operation_kwargs[SEGMENT] = segment
        if total_segments:
            operation_kwargs[TOTAL_SEGMENTS] = total_segments
        if scan_filter:
            conditional_operator = self.get_conditional_operator(conditional_operator or AND)
            filter_expression = await self._get_filter_expression(
                table_name, scan_filter, conditional_operator, name_placeholders, expression_attribute_values)
            operation_kwargs[FILTER_EXPRESSION] = filter_expression
        if consistent_read:
            operation_kwargs[CONSISTENT_READ] = consistent_read
        if name_placeholders:
            operation_kwargs[EXPRESSION_ATTRIBUTE_NAMES] = self._reverse_dict(name_placeholders)
        if expression_attribute_values:
            operation_kwargs[EXPRESSION_ATTRIBUTE_VALUES] = expression_attribute_values

        try:
            return await self.dispatch(SCAN, operation_kwargs)
        except BOTOCORE_EXCEPTIONS as e:
            raise ScanError("Failed to scan table: {0}".format(e), e)

    async def delete_item(self,
                          table_name,
                          hash_key,
                          range_key=None,
                          condition=None,
                          expected=None,
                          conditional_operator=None,
                          return_values=None,
                          return_consumed_capacity=None,
                          return_item_collection_metrics=None):
        """
        Performs the DeleteItem operation and returns the result
        """
        self._check_condition('condition', condition, expected, conditional_operator)

        operation_kwargs = {TABLE_NAME: table_name}
        operation_kwargs.update(await self.get_identifier_map(table_name, hash_key, range_key))
        name_placeholders = {}
        expression_attribute_values = {}

        if condition is not None:
            condition_expression = condition.serialize(name_placeholders, expression_attribute_values)
            operation_kwargs[CONDITION_EXPRESSION] = condition_expression
        if return_values:
            operation_kwargs.update(self.get_return_values_map(return_values))
        if return_consumed_capacity:
            operation_kwargs.update(self.get_consumed_capacity_map(return_consumed_capacity))
        if return_item_collection_metrics:
            operation_kwargs.update(self.get_item_collection_map(return_item_collection_metrics))
        # We read the conditional operator even without expected passed in to maintain existing behavior.
        conditional_operator = self.get_conditional_operator(conditional_operator or AND)
        if expected:
            condition_expression = await self._get_condition_expression(
                table_name, expected, conditional_operator, name_placeholders, expression_attribute_values)
            operation_kwargs[CONDITION_EXPRESSION] = condition_expression
        if name_placeholders:
            operation_kwargs[EXPRESSION_ATTRIBUTE_NAMES] = self._reverse_dict(name_placeholders)
        if expression_attribute_values:
            operation_kwargs[EXPRESSION_ATTRIBUTE_VALUES] = expression_attribute_values

        try:
            return await self.dispatch(DELETE_ITEM, operation_kwargs)
        except BOTOCORE_EXCEPTIONS as e:
            raise DeleteError("Failed to delete item: {0}".format(e), e)

    async def get_identifier_map(self, table_name, hash_key, range_key=None, key=KEY):
        """
        Builds the identifier map that is common to several operations
        """
        tbl = await self.get_meta_table(table_name)
        if tbl is None:
            raise TableError("No such table {0}".format(table_name))
        return tbl.get_identifier_map(hash_key, range_key=range_key, key=key)

    async def describe_table(self, table_name):
        """
        Performs the DescribeTable operation
        """
        try:
            tbl = await self.get_meta_table(table_name, refresh=True)
            if tbl:
                return tbl.data
        except ValueError:
            pass
        raise TableDoesNotExist(table_name)

    async def get_meta_table(self, table_name, refresh=False):
        """
        Returns a MetaTable
        """
        if table_name not in self._tables or refresh:
            operation_kwargs = {
                TABLE_NAME: table_name
            }
            try:
                data = await self.dispatch(DESCRIBE_TABLE, operation_kwargs)
                self._tables[table_name] = MetaTable(data.get(TABLE_KEY))
            except BotoCoreError as e:
                raise TableError("Unable to describe table: {0}".format(e), e)
            except ClientError as e:
                if 'ResourceNotFound' in e.response['Error']['Code']:
                    raise TableDoesNotExist(e.response['Error']['Message'])
                else:
                    raise

        return self._tables[table_name]

    def get_conditional_operator(self, operator):
        """
        Returns a dictionary containing the correct conditional operator,
        validating it first.
        """
        operator = operator.upper()
        if operator not in CONDITIONAL_OPERATORS:
            raise ValueError(
                "The {0} must be one of {1}".format(
                    CONDITIONAL_OPERATOR,
                    CONDITIONAL_OPERATORS
                )
            )
        return {
            CONDITIONAL_OPERATOR: operator
        }

    async def get_item_attribute_map(self, table_name, attributes, item_key=ITEM, pythonic_key=True):
        """
        Builds up a dynamodb compatible AttributeValue map
        """
        tbl = await self.get_meta_table(table_name)
        if tbl is None:
            raise TableError("No such table {0}".format(table_name))
        return tbl.get_item_attribute_map(
            attributes,
            item_key=item_key,
            pythonic_key=pythonic_key)

    def get_consumed_capacity_map(self, return_consumed_capacity):
        """
        Builds the consumed capacity map that is common to several operations
        """
        if return_consumed_capacity.upper() not in RETURN_CONSUMED_CAPACITY_VALUES:
            raise ValueError("{0} must be one of {1}".format(RETURN_ITEM_COLL_METRICS, RETURN_CONSUMED_CAPACITY_VALUES))
        return {
            RETURN_CONSUMED_CAPACITY: str(return_consumed_capacity).upper()
        }

    async def batch_get_item(self,
                             table_name,
                             keys,
                             consistent_read=None,
                             return_consumed_capacity=None,
                             attributes_to_get=None):
        """
        Performs the batch get item operation
        """
        operation_kwargs = {
            REQUEST_ITEMS: {
                table_name: {}
            }
        }

        args_map = {}
        name_placeholders = {}
        if consistent_read:
            args_map[CONSISTENT_READ] = consistent_read
        if return_consumed_capacity:
            operation_kwargs.update(self.get_consumed_capacity_map(return_consumed_capacity))
        if attributes_to_get is not None:
            projection_expression = create_projection_expression(attributes_to_get, name_placeholders)
            args_map[PROJECTION_EXPRESSION] = projection_expression
        if name_placeholders:
            args_map[EXPRESSION_ATTRIBUTE_NAMES] = self._reverse_dict(name_placeholders)
        operation_kwargs[REQUEST_ITEMS][table_name].update(args_map)

        keys_map = {KEYS: []}
        for key in keys:
            keys_map[KEYS].append(
                (await self.get_item_attribute_map(table_name, key))[ITEM]
            )
        operation_kwargs[REQUEST_ITEMS][table_name].update(keys_map)
        try:
            return await self.dispatch(BATCH_GET_ITEM, operation_kwargs)
        except BOTOCORE_EXCEPTIONS as e:
            raise GetError("Failed to batch get items: {0}".format(e), e)

    async def get_item(self,
                       table_name,
                       hash_key,
                       range_key=None,
                       consistent_read=False,
                       attributes_to_get=None):
        """
        Performs the GetItem operation and returns the result
        """
        operation_kwargs = {}
        name_placeholders = {}
        if attributes_to_get is not None:
            projection_expression = create_projection_expression(attributes_to_get, name_placeholders)
            operation_kwargs[PROJECTION_EXPRESSION] = projection_expression
        if name_placeholders:
            operation_kwargs[EXPRESSION_ATTRIBUTE_NAMES] = self._reverse_dict(name_placeholders)
        operation_kwargs[CONSISTENT_READ] = consistent_read
        operation_kwargs[TABLE_NAME] = table_name
        operation_kwargs.update(await self.get_identifier_map(table_name, hash_key, range_key))
        try:
            return await self.dispatch(GET_ITEM, operation_kwargs)
        except BOTOCORE_EXCEPTIONS as e:
            raise GetError("Failed to get item: {0}".format(e), e)

    async def put_item(self,
                       table_name,
                       hash_key,
                       range_key=None,
                       attributes=None,
                       condition=None,
                       expected=None,
                       conditional_operator=None,
                       return_values=None,
                       return_consumed_capacity=None,
                       return_item_collection_metrics=None):
        """
        Performs the PutItem operation and returns the result
        """
        self._check_condition('condition', condition, expected, conditional_operator)

        operation_kwargs = {TABLE_NAME: table_name}
        operation_kwargs.update(await self.get_identifier_map(table_name, hash_key, range_key, key=ITEM))
        name_placeholders = {}
        expression_attribute_values = {}

        if attributes:
            attrs = await self.get_item_attribute_map(table_name, attributes)
            operation_kwargs[ITEM].update(attrs[ITEM])
        if condition is not None:
            condition_expression = condition.serialize(name_placeholders, expression_attribute_values)
            operation_kwargs[CONDITION_EXPRESSION] = condition_expression
        if return_consumed_capacity:
            operation_kwargs.update(self.get_consumed_capacity_map(return_consumed_capacity))
        if return_item_collection_metrics:
            operation_kwargs.update(self.get_item_collection_map(return_item_collection_metrics))
        if return_values:
            operation_kwargs.update(self.get_return_values_map(return_values))
        # We read the conditional operator even without expected passed in to maintain existing behavior.
        conditional_operator = self.get_conditional_operator(conditional_operator or AND)
        if expected:
            condition_expression = await self._get_condition_expression(
                table_name, expected, conditional_operator, name_placeholders, expression_attribute_values)
            operation_kwargs[CONDITION_EXPRESSION] = condition_expression
        if name_placeholders:
            operation_kwargs[EXPRESSION_ATTRIBUTE_NAMES] = self._reverse_dict(name_placeholders)
        if expression_attribute_values:
            operation_kwargs[EXPRESSION_ATTRIBUTE_VALUES] = expression_attribute_values

        try:
            return await self.dispatch(PUT_ITEM, operation_kwargs)
        except BOTOCORE_EXCEPTIONS as e:
            raise PutError("Failed to put item: {0}".format(e), e)

    async def update_item(self,
                          table_name,
                          hash_key,
                          range_key=None,
                          actions=None,
                          attribute_updates=None,
                          condition=None,
                          expected=None,
                          return_consumed_capacity=None,
                          conditional_operator=None,
                          return_item_collection_metrics=None,
                          return_values=None):
        """
        Performs the UpdateItem operation
        """
        self._check_actions(actions, attribute_updates)
        self._check_condition('condition', condition, expected, conditional_operator)

        operation_kwargs = {TABLE_NAME: table_name}
        operation_kwargs.update(await self.get_identifier_map(table_name, hash_key, range_key))
        name_placeholders = {}
        expression_attribute_values = {}

        if condition is not None:
            condition_expression = condition.serialize(name_placeholders, expression_attribute_values)
            operation_kwargs[CONDITION_EXPRESSION] = condition_expression
        if return_consumed_capacity:
            operation_kwargs.update(self.get_consumed_capacity_map(return_consumed_capacity))
        if return_item_collection_metrics:
            operation_kwargs.update(self.get_item_collection_map(return_item_collection_metrics))
        if return_values:
            operation_kwargs.update(self.get_return_values_map(return_values))
        if not actions and not attribute_updates:
            raise ValueError("{0} cannot be empty".format(ATTR_UPDATES))
        actions = actions or []
        attribute_updates = attribute_updates or {}

        update_expression = Update(*actions)
        # We sort the keys here for determinism. This is mostly done to simplify testing.
        for key in sorted(attribute_updates.keys()):
            path = Path([key])
            update = attribute_updates[key]
            action = update.get(ACTION)
            if action not in ATTR_UPDATE_ACTIONS:
                raise ValueError("{0} must be one of {1}".format(ACTION, ATTR_UPDATE_ACTIONS))
            value = update.get(VALUE)
            attr_type, value = self.parse_attribute(value, return_type=True)
            if attr_type is None and action != DELETE:
                attr_type = await self.get_attribute_type(table_name, key, value)
            value = {attr_type: value}
            if action == DELETE:
                action = path.remove() if attr_type is None else path.delete(value)
            elif action == PUT:
                action = path.set(value)
            else:
                action = path.add(value)
            update_expression.add_action(action)
        operation_kwargs[UPDATE_EXPRESSION] = update_expression.serialize(name_placeholders,
                                                                          expression_attribute_values)

        # We read the conditional operator even without expected passed in to maintain existing behavior.
        conditional_operator = self.get_conditional_operator(conditional_operator or AND)
        if expected:
            condition_expression = await self._get_condition_expression(
                table_name, expected, conditional_operator, name_placeholders, expression_attribute_values)
            operation_kwargs[CONDITION_EXPRESSION] = condition_expression
        if name_placeholders:
            operation_kwargs[EXPRESSION_ATTRIBUTE_NAMES] = self._reverse_dict(name_placeholders)
        if expression_attribute_values:
            operation_kwargs[EXPRESSION_ATTRIBUTE_VALUES] = expression_attribute_values

        try:
            return await self.dispatch(UPDATE_ITEM, operation_kwargs)
        except BOTOCORE_EXCEPTIONS as e:
            raise UpdateError("Failed to update item: {0}".format(e), e)

    async def delete_table(self, table_name):
        """
        Performs the DeleteTable operation
        """
        operation_kwargs = {
            TABLE_NAME: table_name
        }
        try:
            data = await self.dispatch(DELETE_TABLE, operation_kwargs)
        except BOTOCORE_EXCEPTIONS as e:
            raise TableError("Failed to delete table: {0}".format(e), e)
        return data

    async def update_table(self,
                           table_name,
                           read_capacity_units=None,
                           write_capacity_units=None,
                           global_secondary_index_updates=None):
        """
        Performs the UpdateTable operation
        """
        operation_kwargs = {
            TABLE_NAME: table_name
        }
        if read_capacity_units and not write_capacity_units or write_capacity_units and not read_capacity_units:
            raise ValueError("read_capacity_units and write_capacity_units are required together")
        if read_capacity_units and write_capacity_units:
            operation_kwargs[PROVISIONED_THROUGHPUT] = {
                READ_CAPACITY_UNITS: read_capacity_units,
                WRITE_CAPACITY_UNITS: write_capacity_units
            }
        if global_secondary_index_updates:
            global_secondary_indexes_list = []
            for index in global_secondary_index_updates:
                global_secondary_indexes_list.append({
                    UPDATE: {
                        INDEX_NAME: index.get(pythonic(INDEX_NAME)),
                        PROVISIONED_THROUGHPUT: {
                            READ_CAPACITY_UNITS: index.get(pythonic(READ_CAPACITY_UNITS)),
                            WRITE_CAPACITY_UNITS: index.get(pythonic(WRITE_CAPACITY_UNITS))
                        }
                    }
                })
            operation_kwargs[GLOBAL_SECONDARY_INDEX_UPDATES] = global_secondary_indexes_list
        try:
            return await self.dispatch(UPDATE_TABLE, operation_kwargs)
        except BOTOCORE_EXCEPTIONS as e:
            raise TableError("Failed to update table: {0}".format(e), e)

    def _check_condition(self, name, condition, expected_or_filter, conditional_operator):
        if condition is not None:
            if not isinstance(condition, Condition):
                raise ValueError("'{0}' must be an instance of Condition".format(name))
            if expected_or_filter or conditional_operator is not None:
                raise ValueError("Legacy conditional parameters cannot be used with condition expressions")
        else:
            if expected_or_filter or conditional_operator is not None:
                warnings.warn("Legacy conditional parameters are deprecated in favor of condition expressions")

    def _check_actions(self, actions, attribute_updates):
        if actions is not None:
            if attribute_updates is not None:
                raise ValueError("Legacy attribute updates cannot be used with update actions")
        else:
            if attribute_updates is not None:
                warnings.warn("Legacy attribute updates are deprecated in favor of update actions")

    def get_return_values_map(self, return_values):
        """
        Builds the return values map that is common to several operations
        """
        if return_values.upper() not in RETURN_VALUES_VALUES:
            raise ValueError("{0} must be one of {1}".format(RETURN_VALUES, RETURN_VALUES_VALUES))
        return {
            RETURN_VALUES: str(return_values).upper()
        }

    async def create_table(self,
                           table_name,
                           attribute_definitions=None,
                           key_schema=None,
                           read_capacity_units=None,
                           write_capacity_units=None,
                           global_secondary_indexes=None,
                           local_secondary_indexes=None,
                           stream_specification=None):
        """
        Performs the CreateTable operation
        """
        operation_kwargs = {
            TABLE_NAME: table_name,
            PROVISIONED_THROUGHPUT: {
                READ_CAPACITY_UNITS: read_capacity_units,
                WRITE_CAPACITY_UNITS: write_capacity_units
            }
        }
        attrs_list = []
        if attribute_definitions is None:
            raise ValueError("attribute_definitions argument is required")
        for attr in attribute_definitions:
            attrs_list.append({
                ATTR_NAME: attr.get(pythonic(ATTR_NAME)),
                ATTR_TYPE: attr.get(pythonic(ATTR_TYPE))
            })
        operation_kwargs[ATTR_DEFINITIONS] = attrs_list

        if global_secondary_indexes:
            global_secondary_indexes_list = []
            for index in global_secondary_indexes:
                global_secondary_indexes_list.append({
                    INDEX_NAME: index.get(pythonic(INDEX_NAME)),
                    KEY_SCHEMA: sorted(index.get(pythonic(KEY_SCHEMA)), key=lambda x: x.get(KEY_TYPE)),
                    PROJECTION: index.get(pythonic(PROJECTION)),
                    PROVISIONED_THROUGHPUT: index.get(pythonic(PROVISIONED_THROUGHPUT))
                })
            operation_kwargs[GLOBAL_SECONDARY_INDEXES] = global_secondary_indexes_list

        if key_schema is None:
            raise ValueError("key_schema is required")
        key_schema_list = []
        for item in key_schema:
            key_schema_list.append({
                ATTR_NAME: item.get(pythonic(ATTR_NAME)),
                KEY_TYPE: str(item.get(pythonic(KEY_TYPE))).upper()
            })
        operation_kwargs[KEY_SCHEMA] = sorted(key_schema_list, key=lambda x: x.get(KEY_TYPE))

        local_secondary_indexes_list = []
        if local_secondary_indexes:
            for index in local_secondary_indexes:
                local_secondary_indexes_list.append({
                    INDEX_NAME: index.get(pythonic(INDEX_NAME)),
                    KEY_SCHEMA: sorted(index.get(pythonic(KEY_SCHEMA)), key=lambda x: x.get(KEY_TYPE)),
                    PROJECTION: index.get(pythonic(PROJECTION)),
                })
            operation_kwargs[LOCAL_SECONDARY_INDEXES] = local_secondary_indexes_list

        if stream_specification:
            operation_kwargs[STREAM_SPECIFICATION] = {
                STREAM_ENABLED: stream_specification[pythonic(STREAM_ENABLED)],
                STREAM_VIEW_TYPE: stream_specification[pythonic(STREAM_VIEW_TYPE)]
            }

        try:
            data = await self.dispatch(CREATE_TABLE, operation_kwargs)
        except BOTOCORE_EXCEPTIONS as e:
            raise TableError("Failed to create table: {0}".format(e), e)
        return data

    async def list_tables(self, exclusive_start_table_name=None, limit=None):
        """
        Performs the ListTables operation
        """
        operation_kwargs = {}
        if exclusive_start_table_name:
            operation_kwargs.update({
                EXCLUSIVE_START_TABLE_NAME: exclusive_start_table_name
            })
        if limit is not None:
            operation_kwargs.update({
                LIMIT: limit
            })
        try:
            return await self.dispatch(LIST_TABLES, operation_kwargs)
        except BOTOCORE_EXCEPTIONS as e:
            raise TableError("Unable to list tables: {0}".format(e), e)

    @property
    def client(self):
        """
        Returns a aiobotocore dynamodb client
        """
        # botocore has a known issue where it will cache empty credentials
        # https://github.com/boto/botocore/blob/4d55c9b4142/botocore/credentials.py#L1016-L1021
        # if the client does not have credentials, we create a new client
        # otherwise the client is permanently poisoned in the case of metadata service flakiness when using IAM roles
        # TODO session connection check
        if not self._client or (self._client._request_signer and not self._client._request_signer._credentials):
            self._client = self.session.create_client(SERVICE_NAME, self.region, endpoint_url=self.host)
        return self._client

    @staticmethod
    def _reverse_dict(d):
        return dict((v, k) for k, v in six.iteritems(d))


def _convert_binary(attr):
    if BINARY_SHORT in attr:
        attr[BINARY_SHORT] = b64decode(attr[BINARY_SHORT].encode(DEFAULT_ENCODING))
    elif BINARY_SET_SHORT in attr:
        value = attr[BINARY_SET_SHORT]
        if value and len(value):
            attr[BINARY_SET_SHORT] = set(b64decode(v.encode(DEFAULT_ENCODING)) for v in value)
