"""
Can be used to run Bokchoi on the Google Cloud using Google Compute Engines

"""

import os
import sys
import time
import logging
import bokchoi.utils

import googleapiclient.discovery
import googleapiclient.errors

from google.cloud import storage, exceptions


# todo: init logger in other module
logger = logging.getLogger()
logger.setLevel(logging.INFO)
logging.basicConfig(handlers=[logging.StreamHandler()])
logging.getLogger('googleapiclient.discovery_cache').setLevel(logging.ERROR)


class GCP(object):
    """Run Bokchoi on the Google Cloud using Google Compute Engines"""
    def __init__(self, bokchoi_project_name, settings):
        self.project_name = bokchoi_project_name
        self.entry_point = settings.get('EntryPoint')
        self.requirements = settings.get('Requirements')
        self.gcp = self.retrieve_gcp_settings(settings)
        self.compute = self.get_authorized_compute()
        self.storage = self.get_authorized_storage()

    @staticmethod
    def get_authorized_compute():
        """Authorize with default method (implicit env variable) or otherwise
        use the explicit authentication.
        """
        # todo: explicit step
        return googleapiclient.discovery.build('compute', 'v1')

    @staticmethod
    def get_authorized_storage():
        """Authorize with default method (implicit env variable) or otherwise
        use the explicit authentication.
        """
        # todo: add explicit step
        return storage.Client()

    @staticmethod
    def retrieve_gcp_settings(settings):
        """
        Checks the given settings and validates if all required values
        are there and the values are correct.
        :arg settings: a json file with with defined settings
        :return:
        """
        # todo: create try/catch to handle input (or move to other class)
        # settings.map(lambda x: if x is None raise Exception())

        gcp = settings.get('GCP')

        return {
            'project': gcp.get('ProjectId'),
            'region': gcp.get('Region', 'europe-west4'),
            'zone': gcp.get('Zone', 'europe-west4-b'),
            'auth_key': gcp.get('AuthKeyLocation'),
            'bucket': gcp.get('Bucket'),
            'network': gcp.get('Network', 'default'),
            'sub_network': gcp.get('SubNetwork', 'default'),
            'instance_type': gcp.get('InstanceType', 'n1-standard-1'),
            'preemptible': gcp.get('Preemptible', False),
            'disk_space': gcp.get('DiskSpaceGb', 50)
        }

    def list_instances(self):
        """List names of all existing instances"""
        instances = []
        result = self.compute.instances().list(
            project=self.gcp.get('project'),
            zone=self.gcp.get('zone')).execute()
        [instances.append(x['name']) for x in result['items']]
        return instances

    def define_config(self):
        """
        Set up a compute engine configuration based on the user's input
        :return: Defined Compute Engine configuration
        """
        image_response = self.compute.images().getFromFamily(
            project='debian-cloud', family='debian-8').execute()

        machine_type = "zones/{}/machineTypes/{}".format(
            self.gcp.get('zone'), self.gcp.get('instance_type'))

        startup_script = open(
            os.path.join(os.path.dirname(__file__), 'scripts/gcp-startup-script.sh'), 'r').read()

        config = {
            'name': self.project_name,
            'machineType': machine_type,

            'disks': [
                {
                    'boot': True,
                    'autoDelete': True,
                    'initializeParams': {
                        'sourceImage': image_response['selfLink'],
                        'diskSizeGb': self.gcp.get('disk_space')
                    }
                }
            ],
            'scheduling': {
              'preemptible': self.gcp.get('preemptible')
            },

            'networkInterfaces': [{
                'network': 'global/networks/{}'.format(self.gcp.get('network')),
                'subnetwork': '/regions/{}/subnetworks/{}'.format(
                    self.gcp.get('region'), self.gcp.get('sub_network')),
                'accessConfigs': [
                    {'type': 'ONE_TO_ONE_NAT', 'name': 'External NAT'}
                ]
            }],

            'serviceAccounts': [{
                'email': 'default',
                'scopes': [
                    'https://www.googleapis.com/auth/devstorage.read_write',
                    'https://www.googleapis.com/auth/logging.write'
                ]
            }],

            'metadata': {
                'items': [{
                    'key': 'startup-script',
                    'value': startup_script
                }, {
                    'key': 'BUCKET_NAME',
                    'value': self.gcp.get('bucket')
                }, {
                    'key': 'PACKAGE_NAME',
                    'value': '{}-{}.zip'.format(self.project_name, 'package')
                }, {
                    'key': 'ENTRYPOINT',
                    'value': self.entry_point
                }]
            }
        }

        return config

    def create_instance(self):
        """Create a new compute engine"""
        logger.info('Creating instance')
        try:
            return self.compute.instances().insert(
                project=self.gcp.get('project'),
                zone=self.gcp.get('zone'),
                body=self.define_config()).execute()
        except googleapiclient.errors.HttpError as e:
            if 'already exists' in str(e):
                logger.error('instance already exists, returning with exit(1)')
                sys.exit(1)
            else:
                logger.error(e)
                sys.exit(1)

    def delete_instance(self):
        """Remove the created compute engine"""
        logger.info('Deleting instance')
        return self.compute.instances().delete(
            project=self.gcp.get('project'),
            zone=self.gcp.get('zone'),
            instance=self.project_name).execute()

    def wait_for_operation(self, operation):
        """Method which polls the status of the operations and returns when the
        operation is completed.
        :arg operation: a gcp api operation
        """
        if operation is None:
            return

        logger.info('Waiting for operation to finish...')
        while True:
            result = self.compute.zoneOperations().get(
                project=self.gcp.get('project'),
                zone=self.gcp.get('zone'),
                operation=operation['name']).execute()

            if result['status'] == 'DONE':
                if 'error' in result:
                    raise Exception(result['error'])
                return result

            time.sleep(3)

    def create_bucket(self):
        """Create a new storage bucket which will be used for the defined job"""
        logger.info('Creating bucket')
        try:
            bucket = self.storage.create_bucket(self.gcp.get('bucket'))
            return bucket
        except exceptions.Conflict as e:
            if 'You already own this bucket' in str(e):
                logger.info('Bucket with name {} already exists, skipping bucket create step.')
            else:
                logger.error(e)

    def delete_bucket(self):
        """Delete the created bucket"""
        logger.info('Deleting bucket')
        bucket = self.storage.get_bucket(self.gcp.get('bucket'))
        try:
            bucket.delete(force=True)
        except Exception as e:
            # todo: make less generic (google.api_core.exceptions.NotFound)
            logger.info('bucket does not exist, skipping deletion')

    def upload_blob(self, file_name, file_object):
        """Upload file to Google Storage
        :arg file_name: target filename in Google storage
        :arg file_object: zip file object which will be uploaded
        :return: public url of the Google Storage resource
        """
        bucket = self.storage.get_bucket(self.gcp.get('bucket'))
        blob = bucket.blob(file_name)
        blob.upload_from_file(file_object)
        return blob.public_url

    def deploy(self):
        """Deploy package to GCP/Google Storage"""
        logger.info('Uploading package to Google Storage bucket')
        self.create_bucket()
        cwd = os.getcwd()
        package, fingerprint = bokchoi.utils.zip_package(cwd, self.requirements)
        self.upload_blob('{}-{}.zip'.format(self.project_name, 'package'), package)

    def undeploy(self):
        """Undeploy and delete all create d resources"""
        logger.info('Deleting resources which are created on GCP')
        self.delete_bucket()

        delete_instance_op = self.delete_instance()
        self.wait_for_operation(delete_instance_op)
        logger.info('Successfully deleted resources')

    def run(self):
        """Run the uploaded package"""
        create_instance_op = self.create_instance()
        self.wait_for_operation(create_instance_op)
        logger.info('Successfully deployed package and created resources')

