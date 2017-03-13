# Copyright (C) 2014 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import time
from unittest import skip

import fixtures

from nodepool import tests
from nodepool import nodedb
from nodepool import zk
import nodepool.fakeprovider
import nodepool.nodepool


class TestNodepool(tests.DBTestCase):
    log = logging.getLogger("nodepool.TestNodepool")

    def test_node_assignment(self):
        '''
        Successful node launch should have unlocked nodes in READY state
        and assigned to the request.
        '''
        configfile = self.setup_config('node.yaml')
        self._useBuilder(configfile)
        image = self.waitForImage('fake-provider', 'fake-image')

        nodepool.nodepool.LOCK_CLEANUP = 1
        pool = self.useNodepool(configfile, watermark_sleep=1)
        pool.start()

        req = zk.NodeRequest()
        req.state = zk.REQUESTED
        req.node_types.append('fake-label')
        self.zk.storeNodeRequest(req)

        req = self.waitForNodeRequest(req)
        self.assertEqual(req.state, zk.FULFILLED)

        self.assertNotEqual(req.nodes, [])
        for node_id in req.nodes:
            node = self.zk.getNode(node_id)
            self.assertEqual(node.allocated_to, req.id)
            self.assertEqual(node.state, zk.READY)
            self.assertIsNotNone(node.launcher)
            self.assertEqual(node.az, "az1")
            p = "{path}/{id}".format(
                path=self.zk._imageUploadPath(image.image_name,
                                              image.build_id,
                                              image.provider_name),
                id=image.id)
            self.assertEqual(node.image_id, p)
            self.zk.lockNode(node, blocking=False)
            self.zk.unlockNode(node)

        # Verify the cleanup thread removed the lock
        self.assertIsNotNone(
            self.zk.client.exists(self.zk._requestLockPath(req.id))
        )
        self.zk.deleteNodeRequest(req)
        self.waitForNodeRequestLockDeletion(req.id)

    def test_node_assignment_at_quota(self):
        '''
        Successful node launch should have unlocked nodes in READY state
        and assigned to the request.
        '''
        configfile = self.setup_config('node_quota.yaml')
        self._useBuilder(configfile)
        self.waitForImage('fake-provider', 'fake-image')

        nodepool.nodepool.LOCK_CLEANUP = 1
        pool = self.useNodepool(configfile, watermark_sleep=1)
        pool.start()
        self.wait_for_config(pool)

        client = pool.getProviderManager('fake-provider')._getClient()

        # One of the things we want to test is that if spawn many node
        # launches at once, we do not deadlock while the request
        # handler pauses for quota.  To ensure we test that case,
        # pause server creation until we have accepted all of the node
        # requests we submit.  This will ensure that we hold locks on
        # all of the nodes before pausing so that we can validate they
        # are released.
        client.pause_creates = True

        req1 = zk.NodeRequest()
        req1.state = zk.REQUESTED
        req1.node_types.append('fake-label')
        req1.node_types.append('fake-label')
        self.zk.storeNodeRequest(req1)
        req2 = zk.NodeRequest()
        req2.state = zk.REQUESTED
        req2.node_types.append('fake-label')
        req2.node_types.append('fake-label')
        self.zk.storeNodeRequest(req2)

        req1 = self.waitForNodeRequest(req1, (zk.PENDING,))
        req2 = self.waitForNodeRequest(req2, (zk.PENDING,))

        # At this point, we should be about to create or have already
        # created two servers for the first request, and the request
        # handler has accepted the second node request but paused
        # waiting for the server count to go below quota.

        # Wait until both of the servers exist.
        while len(client._server_list) < 2:
            time.sleep(0.1)

        # Allow the servers to finish being created.
        for server in client._server_list:
            server.event.set()

        self.log.debug("Waiting for 1st request %s", req1.id)
        req1 = self.waitForNodeRequest(req1)
        self.assertEqual(req1.state, zk.FULFILLED)
        self.assertEqual(len(req1.nodes), 2)

        # Mark the first request's nodes as USED, which will get them deleted
        # and allow the second to proceed.
        self.log.debug("Deleting 1st request %s", req1.id)
        for node_id in req1.nodes:
            node = self.zk.getNode(node_id)
            node.state = zk.USED
            self.zk.storeNode(node)
        self.zk.deleteNodeRequest(req1)
        self.waitForNodeRequestLockDeletion(req1.id)

        # Wait until both of the servers exist.
        while len(client._server_list) < 2:
            time.sleep(0.1)

        # Allow the servers to finish being created.
        for server in client._server_list:
            server.event.set()

        req2 = self.waitForNodeRequest(req2)
        self.assertEqual(req2.state, zk.FULFILLED)
        self.assertEqual(len(req2.nodes), 2)

    def test_fail_request_on_launch_failure(self):
        '''
        Test that provider launch error fails the request.
        '''
        configfile = self.setup_config('node_launch_retry.yaml')
        self._useBuilder(configfile)
        self.waitForImage('fake-provider', 'fake-image')

        pool = self.useNodepool(configfile, watermark_sleep=1)
        pool.start()
        self.wait_for_config(pool)
        manager = pool.getProviderManager('fake-provider')
        manager.createServer_fails = 2

        req = zk.NodeRequest()
        req.state = zk.REQUESTED
        req.node_types.append('fake-label')
        self.zk.storeNodeRequest(req)

        req = self.waitForNodeRequest(req)
        self.assertEqual(0, manager.createServer_fails)
        self.assertEqual(req.state, zk.FAILED)
        self.assertNotEqual(req.declined_by, [])

    def test_invalid_image_fails(self):
        '''
        Test that an invalid image declines and fails the request.
        '''
        configfile = self.setup_config('node.yaml')
        pool = self.useNodepool(configfile, watermark_sleep=1)
        pool.start()

        req = zk.NodeRequest()
        req.state = zk.REQUESTED
        req.node_types.append("zorky-zumba")
        self.zk.storeNodeRequest(req)

        req = self.waitForNodeRequest(req)
        self.assertEqual(req.state, zk.FAILED)
        self.assertNotEqual(req.declined_by, [])

    def test_node(self):
        """Test that an image and node are created"""
        configfile = self.setup_config('node.yaml')
        pool = self.useNodepool(configfile, watermark_sleep=1)
        self._useBuilder(configfile)
        pool.start()
        self.waitForImage('fake-provider', 'fake-image')
        nodes = self.waitForNodes('fake-label')

        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].provider, 'fake-provider')
        self.assertEqual(nodes[0].type, 'fake-label')

    def test_disabled_label(self):
        """Test that a node is not created with min-ready=0"""
        configfile = self.setup_config('node_disabled_label.yaml')
        pool = self.useNodepool(configfile, watermark_sleep=1)
        self._useBuilder(configfile)
        pool.start()
        self.waitForImage('fake-provider', 'fake-image')
        self.assertEqual([], self.zk.getNodeRequests())
        self.assertEqual([], self.zk.getNodes())

    def test_node_net_name(self):
        """Test that a node is created with a net name"""
        configfile = self.setup_config('node_net_name.yaml')
        pool = self.useNodepool(configfile, watermark_sleep=1)
        self._useBuilder(configfile)
        pool.start()
        self.waitForImage('fake-provider', 'fake-image')
        nodes = self.waitForNodes('fake-label')
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].provider, 'fake-provider')
        self.assertEqual(nodes[0].type, 'fake-label')

    def test_node_vhd_image(self):
        """Test that a image and node are created vhd image"""
        configfile = self.setup_config('node_vhd.yaml')
        pool = self.useNodepool(configfile, watermark_sleep=1)
        self._useBuilder(configfile)
        pool.start()
        self.waitForImage('fake-provider', 'fake-image')
        nodes = self.waitForNodes('fake-label')
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].provider, 'fake-provider')
        self.assertEqual(nodes[0].type, 'fake-label')

    def test_node_vhd_and_qcow2(self):
        """Test label provided by vhd and qcow2 images builds"""
        configfile = self.setup_config('node_vhd_and_qcow2.yaml')
        pool = self.useNodepool(configfile, watermark_sleep=1)
        self._useBuilder(configfile)
        self.waitForImage('fake-provider1', 'fake-image')
        self.waitForImage('fake-provider2', 'fake-image')
        pool.start()
        nodes = self.waitForNodes('fake-label', 2)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(zk.READY, nodes[0].state)
        self.assertEqual(zk.READY, nodes[1].state)
        if nodes[0].provider == 'fake-provider1':
            self.assertEqual(nodes[1].provider, 'fake-provider2')
        else:
            self.assertEqual(nodes[1].provider, 'fake-provider1')

    def test_dib_upload_fail(self):
        """Test that an image upload failure is contained."""
        configfile = self.setup_config('node_upload_fail.yaml')
        pool = self.useNodepool(configfile, watermark_sleep=1)
        self._useBuilder(configfile)
        pool.start()
        self.waitForImage('fake-provider2', 'fake-image')
        nodes = self.waitForNodes('fake-label', 2)
        self.assertEqual(len(nodes), 2)
        total_nodes = sum(1 for _ in self.zk.nodeIterator())
        self.assertEqual(total_nodes, 2)
        self.assertEqual(nodes[0].provider, 'fake-provider2')
        self.assertEqual(nodes[0].type, 'fake-label')
        self.assertEqual(nodes[1].provider, 'fake-provider2')
        self.assertEqual(nodes[1].type, 'fake-label')

    def test_node_az(self):
        """Test that an image and node are created with az specified"""
        configfile = self.setup_config('node_az.yaml')
        pool = self.useNodepool(configfile, watermark_sleep=1)
        self._useBuilder(configfile)
        pool.start()
        self.waitForImage('fake-provider', 'fake-image')
        nodes = self.waitForNodes('fake-label')
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].provider, 'fake-provider')
        self.assertEqual(nodes[0].az, 'az1')

    @skip("Disabled for early v3 development")
    def test_node_ipv6(self):
        """Test that a node is created w/ or w/o ipv6 preferred flag"""
        configfile = self.setup_config('node_ipv6.yaml')
        pool = self.useNodepool(configfile, watermark_sleep=1)
        self._useBuilder(configfile)
        pool.start()
        self.waitForImage('fake-provider1', 'fake-image')
        self.waitForImage('fake-provider2', 'fake-image')
        self.waitForImage('fake-provider3', 'fake-image')
        self.waitForNodes(pool)

        with pool.getDB().getSession() as session:
            # ipv6 preferred set to true and ipv6 address available
            nodes = session.getNodes(provider_name='fake-provider1',
                                     label_name='fake-label1',
                                     target_name='fake-target',
                                     state=nodedb.READY)
            self.assertEqual(len(nodes), 1)
            self.assertEqual(nodes[0].ip, 'fake_v6')
            # ipv6 preferred unspecified and ipv6 address available
            nodes = session.getNodes(provider_name='fake-provider2',
                                     label_name='fake-label2',
                                     target_name='fake-target',
                                     state=nodedb.READY)
            self.assertEqual(len(nodes), 1)
            self.assertEqual(nodes[0].ip, 'fake')
            # ipv6 preferred set to true but ipv6 address unavailable
            nodes = session.getNodes(provider_name='fake-provider3',
                                     label_name='fake-label3',
                                     target_name='fake-target',
                                     state=nodedb.READY)
            self.assertEqual(len(nodes), 1)
            self.assertEqual(nodes[0].ip, 'fake')

    def test_node_delete_success(self):
        configfile = self.setup_config('node.yaml')
        pool = self.useNodepool(configfile, watermark_sleep=1)
        self._useBuilder(configfile)
        pool.start()
        self.waitForImage('fake-provider', 'fake-image')
        nodes = self.waitForNodes('fake-label')
        self.assertEqual(len(nodes), 1)
        self.assertEqual(zk.READY, nodes[0].state)
        self.assertEqual('fake-provider', nodes[0].provider)
        nodes[0].state = zk.DELETING
        self.zk.storeNode(nodes[0])

        # Wait for this one to be deleted
        self.waitForNodeDeletion(nodes[0])

        # Wait for a new one to take it's place
        new_nodes = self.waitForNodes('fake-label')
        self.assertEqual(len(new_nodes), 1)
        self.assertEqual(zk.READY, new_nodes[0].state)
        self.assertEqual('fake-provider', new_nodes[0].provider)
        self.assertNotEqual(nodes[0], new_nodes[0])

    def test_node_launch_retries(self):
        configfile = self.setup_config('node_launch_retry.yaml')
        pool = self.useNodepool(configfile, watermark_sleep=1)
        self._useBuilder(configfile)
        pool.start()
        self.wait_for_config(pool)
        manager = pool.getProviderManager('fake-provider')
        manager.createServer_fails = 2
        self.waitForImage('fake-provider', 'fake-image')

        req = zk.NodeRequest()
        req.state = zk.REQUESTED
        req.node_types.append('fake-label')
        self.zk.storeNodeRequest(req)

        req = self.waitForNodeRequest(req)
        self.assertEqual(req.state, zk.FAILED)

        # retries in config is set to 2, so 2 attempts to create a server
        self.assertEqual(0, manager.createServer_fails)

    @skip("Disabled for early v3 development")
    def test_node_delete_failure(self):
        def fail_delete(self, name):
            raise RuntimeError('Fake Error')

        fake_delete = 'nodepool.fakeprovider.FakeJenkins.delete_node'
        self.useFixture(fixtures.MonkeyPatch(fake_delete, fail_delete))

        configfile = self.setup_config('node.yaml')
        pool = self.useNodepool(configfile, watermark_sleep=1)
        self._useBuilder(configfile)
        pool.start()
        self.waitForImage('fake-provider', 'fake-image')
        self.waitForNodes(pool)
        node_id = -1
        with pool.getDB().getSession() as session:
            nodes = session.getNodes(provider_name='fake-provider',
                                     label_name='fake-label',
                                     target_name='fake-target',
                                     state=nodedb.READY)
            self.assertEqual(len(nodes), 1)
            node_id = nodes[0].id

        pool.deleteNode(node_id)
        self.wait_for_threads()
        self.waitForNodes(pool)

        with pool.getDB().getSession() as session:
            ready_nodes = session.getNodes(provider_name='fake-provider',
                                           label_name='fake-label',
                                           target_name='fake-target',
                                           state=nodedb.READY)
            deleted_nodes = session.getNodes(provider_name='fake-provider',
                                             label_name='fake-label',
                                             target_name='fake-target',
                                             state=nodedb.DELETE)
            # Make sure we have one node which is a new node
            self.assertEqual(len(ready_nodes), 1)
            self.assertNotEqual(node_id, ready_nodes[0].id)

            # Make sure our old node is in delete state
            self.assertEqual(len(deleted_nodes), 1)
            self.assertEqual(node_id, deleted_nodes[0].id)

    def test_leaked_node(self):
        """Test that a leaked node is deleted"""
        configfile = self.setup_config('leaked_node.yaml')
        pool = self.useNodepool(configfile, watermark_sleep=1)
        self._useBuilder(configfile)
        pool.start()
        self.waitForImage('fake-provider', 'fake-image')
        self.log.debug("Waiting for initial pool...")
        nodes = self.waitForNodes('fake-label')
        self.log.debug("...done waiting for initial pool.")

        # Make sure we have a node built and ready
        self.assertEqual(len(nodes), 1)
        manager = pool.getProviderManager('fake-provider')
        servers = manager.listServers()
        self.assertEqual(len(servers), 1)

        # Delete the node from ZooKeeper, but leave the instance
        # so it is leaked.
        self.log.debug("Delete node db record so instance is leaked...")
        self.zk.deleteNode(nodes[0])
        self.log.debug("...deleted node db so instance is leaked.")

        # Wait for nodepool to replace it
        self.log.debug("Waiting for replacement pool...")
        new_nodes = self.waitForNodes('fake-label')
        self.log.debug("...done waiting for replacement pool.")
        self.assertEqual(len(new_nodes), 1)

        # Wait for the instance to be cleaned up
        self.waitForInstanceDeletion(manager, nodes[0].external_id)

        # Make sure we end up with only one server (the replacement)
        servers = manager.listServers()
        self.assertEqual(len(servers), 1)

    @skip("Disabled for early v3 development")
    def test_building_image_cleanup_on_start(self):
        """Test that a building image is deleted on start"""
        configfile = self.setup_config('node.yaml')
        pool = nodepool.nodepool.NodePool(self.secure_conf, configfile,
                                          watermark_sleep=1)
        try:
            pool.start()
            self.waitForImage(pool, 'fake-provider', 'fake-image')
            self.waitForNodes(pool)
        finally:
            # Stop nodepool instance so that it can be restarted.
            pool.stop()

        with pool.getDB().getSession() as session:
            images = session.getSnapshotImages()
            self.assertEqual(len(images), 1)
            self.assertEqual(images[0].state, nodedb.READY)
            images[0].state = nodedb.BUILDING

        # Start nodepool instance which should delete our old image.
        pool = self.useNodepool(configfile, watermark_sleep=1)
        pool.start()
        # Ensure we have a config loaded for periodic cleanup.
        while not pool.config:
            time.sleep(0)
        # Wait for startup to shift state to a state that periodic cleanup
        # will act on.
        while True:
            with pool.getDB().getSession() as session:
                if session.getSnapshotImages()[0].state != nodedb.BUILDING:
                    break
                time.sleep(0)
        # Necessary to force cleanup to happen within the test timeframe
        pool.periodicCleanup()
        self.waitForImage(pool, 'fake-provider', 'fake-image')
        self.waitForNodes(pool)

        with pool.getDB().getSession() as session:
            images = session.getSnapshotImages()
            self.assertEqual(len(images), 1)
            self.assertEqual(images[0].state, nodedb.READY)
            # should be second image built.
            self.assertEqual(images[0].id, 2)

    @skip("Disabled for early v3 development")
    def test_building_dib_image_cleanup_on_start(self):
        """Test that a building dib image is deleted on start"""
        configfile = self.setup_config('node.yaml')
        pool = nodepool.nodepool.NodePool(self.secure_conf, configfile,
                                          watermark_sleep=1)
        self._useBuilder(configfile)
        try:
            pool.start()
            self.waitForImage(pool, 'fake-provider', 'fake-image')
            self.waitForNodes(pool)
        finally:
            # Stop nodepool instance so that it can be restarted.
            pool.stop()

        with pool.getDB().getSession() as session:
            # We delete the snapshot image too to force a new dib image
            # to be built so that a new image can be uploaded to replace
            # the image that was in the snapshot table.
            images = session.getSnapshotImages()
            self.assertEqual(len(images), 1)
            self.assertEqual(images[0].state, nodedb.READY)
            images[0].state = nodedb.BUILDING
            images = session.getDibImages()
            self.assertEqual(len(images), 1)
            self.assertEqual(images[0].state, nodedb.READY)
            images[0].state = nodedb.BUILDING

        # Start nodepool instance which should delete our old image.
        pool = self.useNodepool(configfile, watermark_sleep=1)
        pool.start()
        # Ensure we have a config loaded for periodic cleanup.
        while not pool.config:
            time.sleep(0)
        # Wait for startup to shift state to a state that periodic cleanup
        # will act on.
        while True:
            with pool.getDB().getSession() as session:
                if session.getDibImages()[0].state != nodedb.BUILDING:
                    break
                time.sleep(0)
        # Necessary to force cleanup to happen within the test timeframe
        pool.periodicCleanup()
        self.waitForImage(pool, 'fake-provider', 'fake-image')
        self.waitForNodes(pool)

        with pool.getDB().getSession() as session:
            images = session.getDibImages()
            self.assertEqual(len(images), 1)
            self.assertEqual(images[0].state, nodedb.READY)
            # should be second image built.
            self.assertEqual(images[0].id, 2)

    @skip("Disabled for early v3 development")
    def test_job_start_event(self):
        """Test that job start marks node used"""
        configfile = self.setup_config('node.yaml')
        pool = self.useNodepool(configfile, watermark_sleep=1)
        self._useBuilder(configfile)
        pool.start()
        self.waitForImage('fake-provider', 'fake-image')
        self.waitForNodes(pool)

        msg_obj = {'name': 'fake-job',
                   'build': {'node_name': 'fake-label-fake-provider-1'}}
        json_string = json.dumps(msg_obj)
        handler = nodepool.nodepool.NodeUpdateListener(pool,
                                                       'tcp://localhost:8881')
        handler.handleEvent('onStarted', json_string)
        self.wait_for_threads()

        with pool.getDB().getSession() as session:
            nodes = session.getNodes(provider_name='fake-provider',
                                     label_name='fake-label',
                                     target_name='fake-target',
                                     state=nodedb.USED)
            self.assertEqual(len(nodes), 1)

    @skip("Disabled for early v3 development")
    def test_job_end_event(self):
        """Test that job end marks node delete"""
        configfile = self.setup_config('node.yaml')
        pool = self.useNodepool(configfile, watermark_sleep=1)
        self._useBuilder(configfile)
        pool.start()
        self.waitForImage('fake-provider', 'fake-image')
        self.waitForNodes(pool)

        msg_obj = {'name': 'fake-job',
                   'build': {'node_name': 'fake-label-fake-provider-1',
                             'status': 'SUCCESS'}}
        json_string = json.dumps(msg_obj)
        # Don't delay when deleting.
        self.useFixture(fixtures.MonkeyPatch(
            'nodepool.nodepool.DELETE_DELAY',
            0))
        handler = nodepool.nodepool.NodeUpdateListener(pool,
                                                       'tcp://localhost:8881')
        handler.handleEvent('onFinalized', json_string)
        self.wait_for_threads()

        with pool.getDB().getSession() as session:
            node = session.getNode(1)
            self.assertEqual(node, None)

    @skip("Disabled for early v3 development")
    def _test_job_auto_hold(self, result):
        configfile = self.setup_config('node.yaml')
        pool = self.useNodepool(configfile, watermark_sleep=1)
        self._useBuilder(configfile)
        pool.start()

        self.waitForImage('fake-provider', 'fake-image')
        self.waitForNodes(pool)

        with pool.getDB().getSession() as session:
            session.createJob('fake-job', hold_on_failure=1)

        msg_obj = {'name': 'fake-job',
                   'build': {'node_name': 'fake-label-fake-provider-1',
                             'status': result}}
        json_string = json.dumps(msg_obj)
        # Don't delay when deleting.
        self.useFixture(fixtures.MonkeyPatch(
            'nodepool.nodepool.DELETE_DELAY',
            0))
        handler = nodepool.nodepool.NodeUpdateListener(pool,
                                                       'tcp://localhost:8881')
        handler.handleEvent('onFinalized', json_string)
        self.wait_for_threads()
        return pool

    @skip("Disabled for early v3 development")
    def test_job_auto_hold_success(self):
        """Test that a successful job does not hold a node"""
        pool = self._test_job_auto_hold('SUCCESS')
        with pool.getDB().getSession() as session:
            node = session.getNode(1)
            self.assertIsNone(node)

    @skip("Disabled for early v3 development")
    def test_job_auto_hold_failure(self):
        """Test that a failed job automatically holds a node"""
        pool = self._test_job_auto_hold('FAILURE')
        with pool.getDB().getSession() as session:
            node = session.getNode(1)
            self.assertEqual(node.state, nodedb.HOLD)

    @skip("Disabled for early v3 development")
    def test_job_auto_hold_failure_max(self):
        """Test that a failed job automatically holds only one node"""
        pool = self._test_job_auto_hold('FAILURE')
        with pool.getDB().getSession() as session:
            node = session.getNode(1)
            self.assertEqual(node.state, nodedb.HOLD)

        # Wait for a replacement node
        self.waitForNodes(pool)
        with pool.getDB().getSession() as session:
            node = session.getNode(2)
            self.assertEqual(node.state, nodedb.READY)

        # Fail the job again
        msg_obj = {'name': 'fake-job',
                   'build': {'node_name': 'fake-label-fake-provider-2',
                             'status': 'FAILURE'}}
        json_string = json.dumps(msg_obj)
        handler = nodepool.nodepool.NodeUpdateListener(pool,
                                                       'tcp://localhost:8881')
        handler.handleEvent('onFinalized', json_string)
        self.wait_for_threads()

        # Ensure that the second node was deleted
        with pool.getDB().getSession() as session:
            node = session.getNode(2)
            self.assertEqual(node, None)

    def test_label_provider(self):
        """Test that only providers listed in the label satisfy the request"""
        configfile = self.setup_config('node_label_provider.yaml')
        pool = self.useNodepool(configfile, watermark_sleep=1)
        self._useBuilder(configfile)
        pool.start()
        self.waitForImage('fake-provider', 'fake-image')
        self.waitForImage('fake-provider2', 'fake-image')
        nodes = self.waitForNodes('fake-label')
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].provider, 'fake-provider2')
