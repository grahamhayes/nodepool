.. _configuration:

Configuration
=============

Nodepool reads its configuration from ``/etc/nodepool/nodepool.yaml``
by default.  The configuration file follows the standard YAML syntax
with a number of sections defined with top level keys.  For example, a
full configuration file may have the ``providers`` and ``targets``
sections::

  providers:
    ...
  targets:
    ...

The following sections are available.  All are required unless
otherwise indicated.

script-dir
----------
When creating an image to use when launching new nodes, Nodepool will
run a script that is expected to prepare the machine before the
snapshot image is created.  The ``script-dir`` parameter indicates a
directory that holds all of the scripts needed to accomplish this.
Nodepool will copy the entire directory to the machine before invoking
the appropriate script for the image being created.

Example::

  script-dir: /path/to/script/dir

dburi
-----
Indicates the URI for the database connection.  See the `SQLAlchemy
documentation
<http://docs.sqlalchemy.org/en/latest/core/engines.html#database-urls>`_
for the syntax.  Example::

  dburi: 'mysql://nodepool@localhost/nodepool'

cron
----
This section is optional.

Nodepool runs several periodic tasks.  The ``image-update`` task
creates a new image for each of the defined images, typically used to
keep the data cached on the images up to date.  The ``cleanup`` task
deletes old images and servers which may have encountered errors
during their initial deletion.  The ``check`` task attempts to log
into each node that is waiting to be used to make sure that it is
still operational.  The following illustrates how to change the
schedule for these tasks and also indicates their default values::

  cron:
    image-update: '14 2 * * *'
    cleanup: '27 */6 * * *'
    check: '*/15 * * * *'

zmq-publishers
--------------
Lists the ZeroMQ endpoints for the Jenkins masters.  Nodepool uses
this to receive real-time notification that jobs are running on nodes
or are complete and nodes may be deleted.  Example::

  zmq-publishers:
    - tcp://jenkins1.example.com:8888
    - tcp://jenkins2.example.com:8888

gearman-servers
---------------
Lists the Zuul Gearman servers that should be consulted for real-time
demand.  Nodepool will use information from these servers to determine
if additional nodes should be created to satisfy current demand.
Example::

  gearman-servers:
    - host: zuul.example.com
      port: 4730

The ``port`` key is optional.

providers
---------

Lists the OpenStack cloud providers Nodepool should use.  Within each
provider, the Nodepool image types are also defined.  If the resulting
images from different providers should be equivalent, give them the
same name.  Example::

  providers:
    - name: provider1
      username: 'username'
      password: 'password'
      auth-url: 'http://auth.provider1.example.com/'
      project-id: 'project'
      service-type: 'compute'
      service-name: 'compute'
      region-name: 'region1'
      max-servers: 96
      rate: 1.0
      images:
        - name: precise
          base-image: 'Precise'
          min-ram: 8192
          setup: prepare_node.sh
          reset: reset_node.sh
          username: jenkins
          private-key: /var/lib/jenkins/.ssh/id_rsa
        - name: quantal
          base-image: 'Quantal'
          min-ram: 8192
          setup: prepare_node.sh
          reset: reset_node.sh
          username: jenkins
          private-key: /var/lib/jenkins/.ssh/id_rsa
    - name: provider2
      username: 'username'
      password: 'password'
      auth-url: 'http://auth.provider2.example.com/'
      project-id: 'project'
      service-type: 'compute'
      service-name: 'compute'
      region-name: 'region1'
      max-servers: 96
      rate: 1.0
      images:
        - name: precise
          base-image: 'Fake Precise'
          min-ram: 8192
          setup: prepare_node.sh
          reset: reset_node.sh
          username: jenkins
          private-key: /var/lib/jenkins/.ssh/id_rsa

For providers, the `name`, `username`, `password`, `auth-url`,
`project-id`, and `max-servers` keys are required.  For images, the
`name`, `base-image`, and `min-ram` keys are required.  The `username`
and `private-key` values default to the values indicated.  Nodepool
expects that user to exist after running the script indicated by
`setup`.

targets
-------

Lists the Jenkins masters to which Nodepool should attach nodes after
they are created.  Within each target, the images that are used to
create nodes for that target are listed (so different targets may
receive nodes based on either the same or different images).
Example::

  targets:
    - name: jenkins1
      jenkins:
        url: https://jenkins1.example.org/
        user: username
        apikey: key
        credentials-id: id
      images:
        - name: precise
          providers:
            - name: provider1
              min-ready: 2
            - name: provider2
              min-ready: 2
        - name: quantal
          providers:
            - name: provider1
              min-ready: 4
    - name: jenkins2
      jenkins:
        url: https://jenkins2.example.org/
        user: username
        apikey: key
        credentials-id: id
      images:
        - name: precise
          min-ready: 4
          providers:
            - name: provider1

For targets, the `name` is required.  If using Jenkins, the `url`,
`user`, and `apikey` keys are required.  If the `credentials-id` key
is provided, Nodepool will configure the Jenkins slave to use the
Jenkins credential identified by that ID, otherwise it will use the
username and ssh keys configured in the image.

For images specified for a target, all indicated keys are required.
The name of an image should refer to one of the images specified in
the `provider` section.  Within the image section, a list of providers
should be provided; this indicates which providers should be used to
supply this image to this target.  The `min-ready` field indicates
that Nodepool should try to keep that number of nodes of this image
type ready on this target at all times.