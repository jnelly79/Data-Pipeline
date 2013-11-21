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

"""Manipulate Hadoop cluster on Google Compute Engine."""

import logging
import os
import os.path
import time
import urllib
import uuid

from google.appengine.api import urlfetch

from src.hadoop import datastore
from src.hadoop import gce_api
from src.model import appconfig


def MakeScriptRelativePath(relative_path):
  """Converts file path relative to this script to valid path for OS."""
  return os.path.join(os.path.dirname(__file__), relative_path)


class ClusterSetUpError(Exception):
  """Error during Hadoop cluster set-up."""


class PostprocessStatus(object):
  """Enum for postprocess status."""
  WARMING_UP = 'WARMING_UP'
  POSTPROCESSING = 'POSTPROCESSING'
  READY = 'READY'


class HadoopCluster(object):
  """Class to start Compute Engine server farm for Hadoop cluster.

  This class starts up Compute Engines with appropriate configuration for
  Hadoop cluster.  The server farm consists of 1 'master' and multiple
  'workers'.  Hostnames are set by /etc/hosts so that master and workers
  can recognize each other by hostnames.  The common SSH key is distributed
  so that user hadoop can ssh with each other without password.  (SSH is
  the way Hadoop uses for communication.)
  """

  COMPUTE_STARTUP_SCRIPT = 'startup-script.sh'
  GENERATED_FILES_DIR = 'generated_files'
  MASTER_NAME = 'hadoop-master'
  WORKER_NAME_CORE = 'hadoop-worker'

  # Suffix of persistent disk name appended to the instance name.
  PERSISTENT_DISK_SUFFIX = '-pd'
  # TODO(user): Optimize the size.  100GB persistent disk may be too much
  #     for HDFS metadata, and it costs $10/month.
  # TODO(user): Move to AppConfig when we support persistent disk.
  PERSISTENT_DISK_SIZE_GB = 100
  DONE_FILE = '/var/log/STARTUP_SCRIPT_DONE'

  INSTANCE_ROLES = {
      'master': ['NameNode', 'JobTracker'],
      'worker': ['DataNode', 'TaskTracker'],
  }

  HADOOP_PATCH = 'hadoop-1.2.1.patch'

  INSTANCE_IP_ADDRESS_CHECK_INTERVAL = 5
  INSTANCE_IP_ADDRESS_MAX_CHECK_TIMES = 60
  MASTER_WARM_UP_CHECK_INTERVAL = 15
  MASTER_WARM_UP_MAX_CHECK_TIMES = 20
  CLUSTER_SHUTDOWN_CHECK_INTERVAL = 5
  CLUSTER_SHUTDOWN_MAX_CHECK_TIMES = 60

  # Parameters except self and http are referred by locals().
  # pylint: disable=unused-argument
  def __init__(self, http, cluster_id=None,
               project='', name='', prefix='managed', zone='',
               machinetype='', image='', network='',
               num_workers=5, custom_command=''):
    self.postprocess_rpcs = []
    self.authorized_http = http
    self.api = None
    self.enable_hbase = 0

    self.cluster_info = None
    if cluster_id:
      self.cluster_info = datastore.ClusterInfo.get_by_id(cluster_id)

    for item in ('prefix', 'project', 'name', 'zone', 'machinetype',
                 'image', 'network', 'num_workers', 'custom_command'):
      if self.cluster_info:
        setattr(self, item, getattr(self.cluster_info, item, None))
      else:
        setattr(self, item, locals().get(item, None))

    # Machine type with scratch disk.
    self.machinetype_scratch_disk = '%s-d' % self.machinetype
    # Machine type with persistent disk.
    self.machinetype_persistent_disk = self.machinetype

    self.master_name = self.prefix + '-' + self.MASTER_NAME
    self.worker_name_template = '%s-%s-%%03d' % (
        self.prefix, self.WORKER_NAME_CORE)
    self.worker_name_pattern = '^%s-%s-\\d+$' % (
        self.prefix, self.WORKER_NAME_CORE)

    self.instances = {}  # instance names: datastore.InstanceInfo

  def _GetApi(self):
    if not self.api:
      self.api = gce_api.GceApi(self.project, self.zone,
                                authorized_http=self.authorized_http)
    return self.api

  def _GetTmpCloudStorage(self):
    return '%s/hadoop' % appconfig.AppConfig.GetAppConfig().cloud_storage_bucket

  def _MaybeCreatePersistentDisk(self, disk_name):
    """Create a new persistent disk if the disk doesn't exist.

    The method first checks the existence of the disk.  If the disk with the
    same name already exists, the method does nothing further.

    Args:
      disk_name: Name of the new persistent disk.
    Raises:
      ClusterSetUpError: upon error on persistent disk creation.
    """
    # Check if persistent disk already exists.
    disk_info = self._GetApi().GetDisk(disk_name)
    # If the persistent disk doesn't exist, create one now.
    if not disk_info:
      logging.info('Create persistent disk %s', disk_name)
      if not self._GetApi().CreatePersistentDisk(
          disk_name, self.PERSISTENT_DISK_SIZE_GB, wait=True):
        raise ClusterSetUpError('Failed to create persistent disk %s' %
                                disk_name)
    else:
      logging.info('Use existing persistent disk %s for NameNode.',
                   disk_name)

  def _StartInstance(self, instance_name, role, persistent_disk=False):
    """Starts single Compute Engine instance.

    If persistent disk is specified, the new instance has that persistent disk
    attached.  In this case, the instance does not have additional scratch disk.
    If persistent disk is not specified, the new instance has an additional
    scratch disk.

    If the persistent disk does not exist, this method creates one.

    Args:
      instance_name: Name of the new instance.
      role: Instance role name.  Must be one of the keys of INSTANCE_ROLES.
      persistent_disk: Boolean to indicate whether to attach persistent disk.
          If persistent_disk is True, persistent disk with name
          "instance_name-pd" is attached.
          e.g. hadoop-hm-0-pd for instance hadoop-hm-0
    Raises:
      ClusterSetUpError: if the method fails to start an instance.
    """
    persistent_disk_name = None
    if persistent_disk:
      persistent_disk_name = instance_name + self.PERSISTENT_DISK_SUFFIX
      self._MaybeCreatePersistentDisk(persistent_disk_name)

      # With persistent disk, use machine type without scratch disk.
      machinetype = self.machinetype_persistent_disk
      disk_id = persistent_disk_name
    else:
      machinetype = self.machinetype_scratch_disk
      disk_id = 'ephemeral-disk-0'

    logging.info('Starting instance: %s', instance_name)

    rpckey = str(uuid.uuid4())

    metadata = {
        'startup-script-url': '%s/%s' % (
            self._GetTmpCloudStorage(), self.COMPUTE_STARTUP_SCRIPT),
        'rpckey': rpckey,
        'disk-id': 'google-%s' % disk_id,
        'hostname-prefix': self.prefix,
        'num-workers': self.num_workers,
        'hadoop-master': self.master_name,
        'hadoop-worker-template': self.worker_name_template,
        'tmp-cloud-storage': self._GetTmpCloudStorage(),
        'custom-command': self.custom_command,
        'hadoop-patch': self.HADOOP_PATCH,
    }

    if role not in self.INSTANCE_ROLES:
      raise ClusterSetUpError('Invalid instance role name: %s' % role)
    for command in self.INSTANCE_ROLES[role]:
      metadata[command] = 1

    logging.info('Create instance: %s', instance_name)
    if not self._GetApi().CreateInstance(
        instance_name,
        self.network,
        machinetype,
        self.image,
        persistent_disk=persistent_disk_name,
        # startup_script=self.startup_script,
        service_accounts=[
            'https://www.googleapis.com/auth/devstorage.full_control'],
        metadata=metadata):
      raise ClusterSetUpError('Failed to start instance %s' % instance_name)
    instance_status = datastore.InstanceInfo(
        parent=self.cluster_info.key, name=instance_name, role=role,
        rpckey=rpckey)
    instance_status.put()
    if role == 'master':
      self.cluster_info.SetMasterInstance(instance_status)
    self.instances[instance_name] = instance_status

  def _HttpRequest(self, host, path=''):
    """Sends HTTP request to the instance.

    Args:
      host: Hostname or IP address.
      path: URL path.
    Returns:
      HTTP response object.
    """
    request_url = 'http://%s/%s' % (host, path)
    logging.info('Request: %s', request_url)
    # Avoid cache.
    return urlfetch.fetch(request_url, deadline=300,
                          headers={'Cache-Control': 'max-age=0'})

  def _BuildRpcUrl(self, rpckey, command):
    """Builds URL path for RPC command execution.

    Args:
      rpckey: RPC key for the instance.
      command: Command to execute on the instance in string.
    Returns:
      Encoded URL path for RPC.
    """
    return 'call?' + urllib.urlencode({'command': command, 'key': rpckey})

  def _RunCommandAtRemote(self, instance_name, *command_args):
    """Runs specified script at remote instance."""
    command = ' '.join(command_args)
    logging.debug('Executing on %s: %s', instance_name, command)
    instance_info = datastore.InstanceInfo.GetByName(instance_name)
    url_path = self._BuildRpcUrl(instance_info.rpckey, command)

    return self._HttpRequest(instance_info.external_ip, url_path)

  def _WaitForExternalIp(self, instance_name):
    """Waits for the instance to get external IP and returns it.

    Args:
      instance_name: Name of the Compute Engine instance.
    Returns:
      External IP address in string.
    Raises:
      ClusterSetUpError: External IP assignment times out.
    """
    for _ in xrange(self.INSTANCE_IP_ADDRESS_MAX_CHECK_TIMES):
      instance = self._GetApi().GetInstance(instance_name)
      if instance:
        try:
          return instance['networkInterfaces'][0]['accessConfigs'][0]['natIP']
        except (KeyError, IndexError):
          pass
      time.sleep(self.INSTANCE_IP_ADDRESS_CHECK_INTERVAL)

    raise ClusterSetUpError('External IP address time out for %s' %
                            instance_name)

  def _WaitForInstances(self):
    """Waits for instances in the cluster to start."""
    master_ip = self._WaitForExternalIp(self.master_name)
    self.cluster_info.SetMasterIpAddress(master_ip)
    for i in xrange(self.num_workers):
      worker_name = self._WorkerName(i)
      external_ip = self._WaitForExternalIp(worker_name)
      instance_info = datastore.InstanceInfo.GetByName(worker_name)
      instance_info.external_ip = external_ip
      instance_info.put()

    for _ in xrange(self.MASTER_WARM_UP_MAX_CHECK_TIMES):
      try:
        response = self._RunCommandAtRemote(
            self.master_name, 'cat', '/var/log/STARTUP_SCRIPT_DONE')
        if response.status_code == 200:
          if response.content.find('success') != -1:
            logging.info('Hadoop cluster started successfully.')
            return
          else:
            logging.error(response.content)
            raise ClusterSetUpError('Hadoop master failed to start')
      except urlfetch.DownloadError:
        # RPC daemon on the instance is not yet ready.
        pass
      time.sleep(self.MASTER_WARM_UP_CHECK_INTERVAL)

    raise ClusterSetUpError('Hadoop master set up timed out')

  def _WorkerName(self, index):
    """Returns Hadoop worker name with spedified worker index."""
    return self.worker_name_template % index

  def _StartMaster(self):
    """Starts Hadoop master Compute Engine instance."""
    self._StartInstance(self.master_name, 'master')

  def _StartWorkers(self):
    """Starts Hadoop worker Compute Engine instances."""
    for i in xrange(self.num_workers):
      self._StartInstance(self._WorkerName(i), 'worker')

  def StartHadoopCluster(self):
    """Starts Hadoop cluster on Compute Engine."""
    try:
      self.cluster_info = datastore.ClusterInfo(
          name=self.name, project=self.project, prefix=self.prefix,
          zone=self.zone, machinetype=self.machinetype, image=self.image,
          network=self.network, num_workers=self.num_workers,
          custom_command=self.custom_command)
      self.cluster_info.put()
      self._StartMaster()
      self._StartWorkers()
      self._WaitForInstances()
    except ClusterSetUpError, e:
      self.cluster_info.SetStatus('%s: %s' % (
          datastore.ClusterStatus.ERROR, str(e)))

  def TeardownCluster(self):
    """Deletes Compute Engine instances with likely names."""
    self.cluster_info.SetStatus(datastore.ClusterStatus.TEARING_DOWN)

    while True:
      instances = self._GetApi().ListInstances('name eq "%s|%s"' % (
          self.master_name, self.worker_name_pattern))
      instance_names = [i['name'] for i in instances]
      if not instance_names:
        break
      for name in instance_names:
        logging.info('Shutting down %s', name)
        self._GetApi().DeleteInstance(name)

      for _ in xrange(self.CLUSTER_SHUTDOWN_MAX_CHECK_TIMES):
        still_alive = []
        for name in instance_names:
          instance = self._GetApi().GetInstance(name)
          if instance is not None:
            still_alive.append(name)
        if not still_alive:
          break
        instance_names = still_alive
        time.sleep(self.CLUSTER_SHUTDOWN_CHECK_INTERVAL)

    self.cluster_info.key.delete()
