#!/usr/bin/env python
# Authors: Cedric Bosdonnat <cbosdonnat@suse.com>
#
# Copyright (C) 2017 SUSE, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 675 Mass Ave, Cambridge, MA 02139, USA.
#

import hashlib
import json
import shutil
import tempfile
import getpass
import os
import logging
from subprocess import call, check_call, PIPE, Popen

# default_image_dir - Path where Docker images (tarballs) will be stored
if os.geteuid() == 0:
    virt_sandbox_connection = "lxc:///"
    default_image_dir = "/var/lib/virt-bootstrap/docker_images"
else:
    virt_sandbox_connection = "qemu:///session"
    default_image_dir = \
        os.environ['HOME'] + "/.local/share/virt-bootstrap/docker_images"


def checksum(path, sum_type, sum_expected):
    algorithm = getattr(hashlib, sum_type)
    try:
        fd = open(path, 'rb')
        content = fd.read()
        fd.close()

        actual = algorithm(content).hexdigest()
        return actual == sum_expected
    except Exception:
        return False


def safe_untar(src, dest):
    # Extract tarball in LXC container for safety
    virt_sandbox = ['virt-sandbox',
                    '-c', virt_sandbox_connection,
                    '-m', 'host-bind:/mnt=' + dest]  # Bind destination folder

    # Compression type is auto detected from tar
    # Exclude files under /dev to avoid "Cannot mknod: Operation not permitted"
    params = ['--', '/bin/tar', 'xf', src, '-C', '/mnt', '--exclude', 'dev/*']
    if call(virt_sandbox + params) != 0:
        logging.error(_('virt-sandbox exit with non-zero code. '
                        'Please check if "libvirtd" is running.'))


def get_layer_info(digest, image_dir):
    sum_type, sum_value = digest.split(':')
    layer_file = "{}/{}.tar".format(image_dir, sum_value)
    return (sum_type, sum_value, layer_file)


def untar_layers(layers_list, image_dir, dest_dir):
    for layer in layers_list:
        sum_type, sum_value, layer_file = get_layer_info(layer['digest'],
                                                         image_dir)
        logging.info('Untar layer file: ({}) {}'.format(sum_type, layer_file))

        # Verify the checksum
        if not checksum(layer_file, sum_type, sum_value):
            raise Exception("Digest not matching: " + layer['digest'])

        # Extract layer tarball into destination directory
        safe_untar(layer_file, dest_dir)


def create_qcow2(tar_file, qcow2_backing_file, qcow2_layer_file):
    if not qcow2_backing_file:
        # Create base qcow2 layer
        check_call(['virt-make-fs',
                    '--type=ext3',
                    '--format=qcow2',
                    tar_file,
                    qcow2_layer_file])
    else:
        # Create new qcow2 image with backing chain
        check_call(["qemu-img",
                    "create",
                    "-b", qcow2_backing_file,
                    "-f", "qcow2",
                    qcow2_layer_file])

        # Extract tarball in the new qcow2 image
        if call(["virt-tar-in", "-a", qcow2_layer_file, tar_file, "/"],
                stdout=PIPE,
                stderr=PIPE) != 0:

            # "virt-tar-in" unpacks an uncompressed tarball.
            # If the tarball was compressed the above will exit
            # with returncode 1. In such case use "zcat".
            tar_in = Popen(["virt-tar-in", "-a", qcow2_layer_file, "-", "/"],
                           stdin=PIPE,
                           stdout=PIPE)
            zcat = Popen(["zcat", tar_file],
                         stdout=tar_in.stdin)
            zcat.wait()


def extract_layers_in_qcow2(layers_list, image_dir, dest_dir):
    qcow2_backing_file = None

    for index, layer in enumerate(layers_list):
        # Get layer file information
        sum_type, sum_value, tar_file = \
         get_layer_info(layer['digest'], image_dir)

        logging.info('Untar layer file: ({}) {}'.format(sum_type, tar_file))

        # Verify the checksum
        if not checksum(tar_file, sum_type, sum_value):
            raise Exception("Digest not matching: " + layer['digest'])

        # Name format for the qcow2 image
        qcow2_layer_file = "{}/layer-{}.qcow2".format(dest_dir, index)
        # Create the image layer
        create_qcow2(tar_file, qcow2_backing_file, qcow2_layer_file)
        # Keep the file path for the next layer
        qcow2_backing_file = qcow2_layer_file


class FileSource:
    def __init__(self, url, *args):
        self.path = url.path

    def unpack(self, dest):
        '''
        Safely extract root filesystem from tarball

        @param dest: Directory path where the files to be extraced
        '''
        safe_untar(self.path, dest)


class DockerSource:
    def __init__(self, url, username, password, fmt, insecure, no_cache):
        '''
        Bootstrap root filesystem from Docker registry

        @param url: Address of source registry
        @param username: Username to access source registry
        @param password: Password to access source registry
        @param fmt: Format used to store image [dir, qcow2]
        @param insecure: Do not require HTTPS and certificate verification
        @param no_cache: Whether to store downloaded images or not
        '''

        self.registry = url.netloc
        self.image = url.path
        self.username = username
        self.password = password
        self.output_format = fmt
        self.insecure = insecure
        self.no_cache = no_cache
        if self.image and not self.image.startswith('/'):
            self.image = '/' + self.image
        self.url = "docker://" + self.registry + self.image

    def unpack(self, dest):
        '''
        Extract image files from Docker image

        @param dest: Directory path where the files to be extraced
        '''

        if self.no_cache:
            tmp_dest = tempfile.mkdtemp('virt-bootstrap')
            images_dir = tmp_dest
        else:
            if not os.path.exists(default_image_dir):
                os.makedirs(default_image_dir)
            images_dir = default_image_dir

        try:
            # Run skopeo copy into a tmp folder
            # Note: we don't want to expose --src-cert-dir to users as
            #       they should place the certificates in the system
            #       folders for broader enablement
            skopeo_copy = ["skopeo", "copy", self.url, "dir:"+images_dir]

            if self.insecure:
                skopeo_copy.append('--src-tls-verify=false')
            if self.username:
                if not self.password:
                    self.password = getpass.getpass()
                skopeo_copy.append('--src-creds={}:{}'.format(self.username,
                                                              self.password))
            # Run "skopeo copy" command
            check_call(skopeo_copy)

            # Get the layers list from the manifest
            mf = open(images_dir+"/manifest.json", "r")
            manifest = json.load(mf)

            # Layers are in order - root layer first
            # Reference:
            # https://github.com/containers/image/blob/master/image/oci.go#L100
            if self.output_format == 'dir':
                untar_layers(manifest['layers'], images_dir, dest)
            elif self.output_format == 'qcow2':
                extract_layers_in_qcow2(manifest['layers'], images_dir, dest)
            else:
                raise Exception("Unknown format:" + self.output_format)

        except Exception:
            raise

        else:
            logging.info("Download and extract completed!")
            logging.info("Files are stored in: " + dest)

        finally:
            # Clean up
            if self.no_cache:
                shutil.rmtree(tmp_dest)
