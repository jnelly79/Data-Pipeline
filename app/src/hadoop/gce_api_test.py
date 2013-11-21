# Copyright 2013 Google Inc. All Rights Reserved.
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
# http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests of gce_api.py."""

import apiclient
import gce_api
import mock

from src import basetest


class GceApiTest(basetest.TestCase):
  """Unit test class of GceApi."""

  def setUp(self):
    self.authorized_http = mock.MagicMock()
    self.gce_api = gce_api.GceApi(
        'project-name', 'zone-name', self.authorized_http)
    self.mock_api_build = mock.patch('apiclient.discovery.build').start()

  def tearDown(self):
    mock.patch.stopall()

  def testGetApi(self):
    """Unit test of GetApi().  Make sure authorized_http is used."""
    self.gce_api.GetApi()

    self.assertEqual(1, self.mock_api_build.call_count)
    self.assertEqual(self.authorized_http,
                     self.mock_api_build.call_args[1]['http'])

  def testGetApi_Twice(self):
    """GetApi() called twice, but build() is called once."""
    api1 = self.gce_api.GetApi()
    api2 = self.gce_api.GetApi()

    self.assertEqual(api1, api2)
    self.assertEqual(1, self.mock_api_build.call_count)
    self.assertEqual(self.authorized_http,
                     self.mock_api_build.call_args[1]['http'])

  def testGetInstance(self):
    """Unit test of GetInstance()."""
    instance_info = self.gce_api.GetInstance('instance-name')

    mock_api = self.mock_api_build.return_value
    mock_instance_get = mock_api.instances.return_value.get
    mock_instance_get.assert_called_once_with(
        project='project-name', zone='zone-name', instance='instance-name')
    mock_instance_get.return_value.execute.assert_called_once_with()
    self.assertEqual(mock_instance_get.return_value.execute.return_value,
                     instance_info)

  def testGetInstance_NotFound(self):
    """GetInstance() returns None when the instance is not found."""
    http_error = apiclient.errors.HttpError(None, None, None)
    http_error.resp = {'status': '404'}
    mock_api = self.mock_api_build.return_value
    mock_instance_get = mock_api.instances.return_value.get
    # Configure execute() to throw HttpError.
    mock_instance_get.return_value.execute.side_effect = http_error

    instance_info = self.gce_api.GetInstance('instance-name')

    mock_instance_get.assert_called_once_with(
        project='project-name', zone='zone-name', instance='instance-name')
    mock_instance_get.return_value.execute.assert_called_once_with()
    self.assertIsNone(instance_info)

  def testListInstance_NoFilter(self):
    """Unit test of ListInstance() without filter string."""
    mock_api = self.mock_api_build.return_value
    mock_instances_list = mock_api.instances.return_value.list

    mock_instances_list.return_value.execute.return_value = {
        'items': ['dummy', 'list']
    }

    instance_list = self.gce_api.ListInstances()

    mock_instances_list.assert_called_once_with(
        project='project-name', zone='zone-name', filter=None)
    mock_instances_list.return_value.execute.assert_called_once_with()
    self.assertEqual(['dummy', 'list'], instance_list)

  def testListInstance_Filter(self):
    """Unit test of ListInstance() with filter string."""
    mock_api = self.mock_api_build.return_value
    mock_instances_list = mock_api.instances.return_value.list

    mock_instances_list.return_value.execute.return_value = {
        'items': ['dummy', 'list']
    }

    instance_list = self.gce_api.ListInstances('filter condition')

    mock_instances_list.assert_called_once_with(
        project='project-name', zone='zone-name', filter='filter condition')
    mock_instances_list.return_value.execute.assert_called_once_with()
    self.assertEqual(['dummy', 'list'], instance_list)

  def testCreateInstance_Success(self):
    """Unit test of CreateInstance() with success result."""
    mock_api = self.mock_api_build.return_value
    mock_instance_insert = mock_api.instances.return_value.insert

    mock_instance_insert.return_value.execute.return_value = {
        'name': 'instance-name'
    }

    self.assertTrue(self.gce_api.CreateInstance(
        'instance-name', 'network', 'machine-type', 'image-name', 'zone-name'))

    mock_instance_insert.assert_called_once_with(
        project='project-name', zone='zone-name', body=mock.ANY)
    mock_instance_insert.return_value.execute.assert_called_once_with()

  def testCreateInstance_SuccessWithWarning(self):
    """Unit test of CreateInstance() with warning."""
    mock_api = self.mock_api_build.return_value
    mock_instance_insert = mock_api.instances.return_value.insert

    mock_instance_insert.return_value.execute.return_value = {
        'name': 'instance-name',
        'warnings': [
            {
                'code': 'some warning code',
                'message': 'some warning message'
            }
        ]
    }

    # CreateInstance() returns True for warning.
    self.assertTrue(self.gce_api.CreateInstance(
        'instance-name', 'network', 'machine-type', 'image-name', 'zone-name'))

    mock_instance_insert.assert_called_once_with(
        project='project-name', zone='zone-name', body=mock.ANY)
    mock_instance_insert.return_value.execute.assert_called_once_with()

  def testCreateInstance_Error(self):
    """Unit test of CreateInstance() with error."""
    mock_api = self.mock_api_build.return_value
    mock_instance_insert = mock_api.instances.return_value.insert

    mock_instance_insert.return_value.execute.return_value = {
        'name': 'instance-name',
        'error': {
            'errors': [
                {
                    'code': 'some error code',
                    'message': 'some error message'
                }
            ]
        }
    }

    # CreateInstance() returns False.
    self.assertFalse(self.gce_api.CreateInstance(
        'instance-name', 'network', 'machine-type', 'image-name', 'zone-name'))

    mock_instance_insert.assert_called_once_with(
        project='project-name', zone='zone-name', body=mock.ANY)
    mock_instance_insert.return_value.execute.assert_called_once_with()

  def testDeleteInstance(self):
    """Unit test of DeleteInstance()."""
    mock_api = self.mock_api_build.return_value
    mock_instances_delete = mock_api.instances.return_value.delete

    self.assertTrue(self.gce_api.DeleteInstance('instance-name'))

    mock_instances_delete.assert_called_once_with(
        project='project-name', zone='zone-name', instance='instance-name')
    mock_instances_delete.return_value.execute.assert_called_once_with()


if __name__ == '__main__':
  basetest.main()
