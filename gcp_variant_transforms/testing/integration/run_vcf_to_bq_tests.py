# Copyright 2017 Google Inc.  All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

r"""Integration testing runner for Variant Transforms' VCF to BigQuery pipeline.

To define a new integration test case, create a json file in
`gcp_variant_transforms/testing/integration/vcf_to_bq_tests` directory and
specify at least test_name and table_name for the integration
test. You may add multiple test cases (Now at most two are supported) in one
json file, and the second test case will run after the first one finishes.

You may run this test in any project (the test files are publicly accessible).
Execute the following command from the root source directory:
python gcp_variant_transforms/testing/integration/run_vcf_to_bq_tests.py \
  --project myproject \
  --region us-central1 \
  --staging_location gs://mybucket/staging \
  --temp_location gs://mybucket/temp \
  --logging_location gs://mybucket/temp/integration_test_logs

By default, it runs all integration tests inside
`gcp_variant_transforms/testing/integration/vcf_to_bq_tests/presubmit_tests/`.

To keep the tables that this test creates, use the --keep_tables option.

To validate old tables, i.e., skip most of the test (including table creation
and populating) and only do the validation, use --revalidation_dataset_id, e.g.,
--revalidation_dataset_id integration_tests_20180117_151528
"""

import argparse
from concurrent.futures import TimeoutError as ConcurrentTimeoutError
import enum
import os
import sys
import time
from datetime import datetime
from typing import Dict, List  # pylint: disable=unused-import

from apache_beam.io import filesystems

# TODO(bashir2): Figure out why pylint can't find this.
# pylint: disable=no-name-in-module,import-error
from google.cloud import bigquery

from gcp_variant_transforms.libs import bigquery_util
from gcp_variant_transforms.testing.integration import run_tests_common

_TOOL_NAME = 'vcf_to_bq'
_BASE_TEST_FOLDER = 'gcp_variant_transforms/testing/integration/vcf_to_bq_tests'
_NUM_QUERY_RETIRES = 3

class VcfToBQTestCase(run_tests_common.TestCaseInterface):
  """Test case that holds information to run in Pipelines API."""

  def __init__(self,
               context,  # type: TestContextManager
               test_name,  # type: str
               table_name,  # type: str
               assertion_configs,  # type: List[Dict]
               **kwargs  # type: **str
              ):
    # type: (...) -> None
    self._dataset_id = context.dataset_id
    self._table_id = table_name
    dataset_table = '{}.{}'.format(self._dataset_id, self._table_id)
    self._name = test_name
    self._project = context.project
    full_table_id = '{}:{}'.format(self._project, dataset_table)
    self._assertion_configs = assertion_configs
    args = ['--output_table {}'.format(full_table_id),
            '--staging_location {}'.format(context.staging_location),
            '--temp_location {}'.format(context.temp_location),
            '--job_name {}-{}'.format(test_name, self._dataset_id.replace('_',
                                                                          '-'))]
    for k, v in kwargs.items():
      value = v
      if isinstance(v, str):
        value = v.format(TABLE_NAME=full_table_id)
      args.append('--{} {}'.format(k, value))
    self.run_test_command = run_tests_common.form_command(
        context.project,
        context.region,
        filesystems.FileSystems.join(context.logging_location, full_table_id),
        context.image, context.sdk_container_image, _TOOL_NAME, args)

  def validate_result(self):
    """Runs queries against the output table and verifies results."""
    client = bigquery.Client(project=self._project)
    query_formatter = QueryFormatter(self._dataset_id, self._table_id)
    for assertion_config in self._assertion_configs:
      query = query_formatter.format_query(assertion_config['query'])
      assertion = QueryAssertion(client, self._name, query, assertion_config[
          'expected_result'])
      assertion.run_assertion()
      time.sleep(5)  # Avoid overwhelming BQ with too many concurrent queries.

  def get_name(self):
    return self._name


class QueryAssertion():
  """Runs a query and verifies that the output matches the expected result."""

  def __init__(self, client, test_name, query, expected_result):
    # type: (bigquery.Client, str, str, Dict) -> None
    self._client = client
    self._test_name = test_name
    self._query = query
    self._expected_result = expected_result

  def run_assertion(self):
    query_job = self._client.query(self._query)
    num_retries = 0
    while True:
      try:
        results = query_job.result(timeout=300)
      except ConcurrentTimeoutError as e:
        print('WARNING: Time out waiting for query: {}'.format(self._query))
        if num_retries < _NUM_QUERY_RETIRES:
          num_retries += 1
          time.sleep(90)
        else:
          raise e
      else:
        if results.total_rows == 1:
          break
        print('ERROR: Query `{}` did not return expected num rows: {}'.format(
              self._query, results.total_rows))
        if num_retries < _NUM_QUERY_RETIRES:
          num_retries += 1
          time.sleep(90)
        else:
          raise run_tests_common.TestCaseFailure(
              'Expected 1 row query results instead got {} in test {}'.format(
                  results.total_rows, self._test_name))

    row = list(results)[0]
    col_names = list(row.keys())
    if set(self._expected_result.keys()) != set(col_names):
      raise run_tests_common.TestCaseFailure(
          'Expected {} columns in the query result, got {} in test {}'.format(
              list(self._expected_result.keys()), col_names, self._test_name))
    for key in self._expected_result.keys():
      if self._expected_result.get(key) != row.get(key):
        raise run_tests_common.TestCaseFailure(
            'Column {} mismatch: expected {}, got {} in test {}'.format(
                key, self._expected_result[key], row.get(key), self._test_name))


class QueryFormatter():
  """Formats a query.

  Replaces macros and variables in the query.
  """

  class _QueryMacros(enum.Enum):
    NUM_OUTPUT_TABLES = (
        'SELECT COUNT(0) AS num_tables FROM `{DATASET_ID}.__TABLES_SUMMARY__` '
        'WHERE STARTS_WITH(table_id, "{TABLE_ID}' +
        bigquery_util.TABLE_SUFFIX_SEPARATOR + '") '
        'OR STARTS_WITH(table_id, "{TABLE_ID}_samples' +
        bigquery_util.TABLE_SUFFIX_SEPARATOR + '")')

  def __init__(self, dataset_id, table_id):
    # type: (str, str) -> None
    self._dataset_id = dataset_id
    self._table_id = table_id

  def format_query(self, query):
    # type: (List[str]) -> str
    """Formats the given ``query``.

    Formatting logic is as follows:
    - Concatenates ``query`` parts into one string.
    - Replaces macro with the corresponding value defined in _QueryMacros.
    - Replaces variables associated for the query.
    """
    return self._replace_variables(self._replace_macros(' '.join(query)))

  def _replace_variables(self, query):
    return query.format(TABLE_ID=self._table_id,
                        DATASET_ID=self._dataset_id)

  def _replace_macros(self, query):
    for macro in self._QueryMacros:
      if macro.name == query:
        return macro.value
    return query


class TestContextManager():
  """Manages all resources for a given run of tests.

  Responsible for setting up tests (i.e. creating a unique dataset) and
  cleaning up all resources after tests have been completed.
  """

  def __init__(self, args):
    # type: (argparse.ArgumentParser) -> None
    self.staging_location = args.staging_location
    self.temp_location = args.temp_location
    self.logging_location = args.logging_location
    self.project = args.project
    self.region = args.region
    self.image = args.image
    self.sdk_container_image = args.sdk_container_image
    self._keep_tables = args.keep_tables
    self.revalidation_dataset_id = args.revalidation_dataset_id
    if self.revalidation_dataset_id:
      self.dataset_id = self.revalidation_dataset_id
    else:
      self.dataset_id = 'integration_tests_{}'.format(
          datetime.now().strftime('%Y%m%d_%H%M%S'))

  def __enter__(self):
    if not self.revalidation_dataset_id:
      client = bigquery.Client(project=self.project)
      dataset_ref = client.dataset(self.dataset_id)
      dataset = bigquery.Dataset(dataset_ref)
      dataset.location = 'US'
      _ = client.create_dataset(dataset)  # See #171, pylint: disable=no-member
    return self

  def __exit__(self, *args):
    # See #171 for why we need: pylint: disable=no-member
    if not self._keep_tables:
      client = bigquery.Client(project=self.project)
      dataset_ref = client.dataset(self.dataset_id)
      dataset = bigquery.Dataset(dataset_ref)
      client.delete_dataset(dataset, delete_contents=True)


def _get_args():
  parser = argparse.ArgumentParser()
  run_tests_common.add_args(parser)
  parser.add_argument('--run_presubmit_tests',
                      type=bool, default=False, nargs='?', const=True,
                      help='If set, runs the presubmit_tests.')
  parser.add_argument('--run_all_tests',
                      type=bool, default=False, nargs='?', const=True,
                      help='If set, runs all integration tests.')
  parser.add_argument('--keep_tables',
                      type=bool, default=False, nargs='?', const=True,
                      help='If set, created tables are not deleted.')
  parser.add_argument(
      '--revalidation_dataset_id',
      help=('If set, instead of running the full test, skips '
            'most of it and only validates the tables in the '
            'given dataset. This is useful when --keep_tables '
            'is used in a previous run. Example: '
            '--revalidation_dataset_id integration_tests_20180118_014812'))
  parser.add_argument(
      '--test_file_suffix',
      default='',
      help=('If provided, only the test files in `vcf_to_bq_tests` '
            'that end with the provided string (must include the file '
            'extension) will run.'))
  parser.add_argument(
      '--test_name_prefix',
      default='',
      help=('If provided, all test names will have this prefix. Mainly, to '
            'distinguish the integration tests run by cloudbuild_CI.'))
  return parser.parse_args()


def _get_test_configs(run_presubmit_tests, run_all_tests, test_file_suffix=''):
  # type: (bool, bool, str) -> List[List[Dict]]
  """Gets all test configs."""
  required_keys = ['test_name', 'table_name', 'assertion_configs']
  test_file_path = _get_test_file_path(run_presubmit_tests, run_all_tests,
                                       test_file_suffix)
  test_configs = run_tests_common.get_configs(test_file_path,
                                              required_keys,
                                              test_file_suffix)
  for test_case_configs in test_configs:
    for test_config in test_case_configs:
      assertion_configs = test_config['assertion_configs']
      for assertion_config in assertion_configs:
        _validate_assertion_config(assertion_config)
  return test_configs


def _get_test_file_path(run_presubmit_tests,
                        run_all_tests,
                        test_file_suffix=''):
  # type: (bool, bool, str) -> str
  if run_all_tests or test_file_suffix:
    test_file_path = os.path.join(os.getcwd(), _BASE_TEST_FOLDER)
  elif run_presubmit_tests:
    test_file_path = os.path.join(
        os.getcwd(), _BASE_TEST_FOLDER, 'presubmit_tests')
  else:
    test_file_path = os.path.join(
        os.getcwd(), _BASE_TEST_FOLDER, 'presubmit_tests/small_tests')
  return test_file_path


def _validate_assertion_config(assertion_config):
  required_keys = ['query', 'expected_result']
  for key in required_keys:
    if key not in assertion_config:
      raise ValueError('Test case in {} is missing required key: {}'.format(
          assertion_config, key))


def main():
  args = _get_args()
  test_configs = _get_test_configs(
      args.run_presubmit_tests, args.run_all_tests, args.test_file_suffix)
  with TestContextManager(args) as context:
    tests = []
    for test_case_configs in test_configs:
      test_cases = []
      for config in test_case_configs:
        if args.test_name_prefix:
          config['test_name'] = args.test_name_prefix + config['test_name']
        test_cases.append(VcfToBQTestCase(context, **config))
      tests.append(test_cases)
    test_runner = run_tests_common.TestRunner(
        tests, context.revalidation_dataset_id is not None)
    test_runner.run()
  return test_runner.print_results()


if __name__ == '__main__':
  print('Starting vcf_to_bq tests...')
  ret_code = main()
  print('Finished all vcf_to_bq tests successfully.')
  sys.exit(ret_code)
