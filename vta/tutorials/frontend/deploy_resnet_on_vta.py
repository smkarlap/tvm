# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""
Deploy Pretrained ResNet Model from MxNet on VTA
================================================
**Author**: `Thierry Moreau <https://homes.cs.washington.edu/~moreau/>`_

This tutorial provides an end-to-end demo, on how to run ResNet-18 inference
onto the VTA accelerator design to perform ImageNet classification tasks.
It showcases Relay as a front end compiler that can perform quantization (VTA
only supports int8/32 inference) as well as graph packing (in order to enable
tensorization in the core) to massage the compute graph for the hardware target.
"""

######################################################################
# Install dependencies
# --------------------
# To use the autotvm package in tvm, we need to install some extra dependencies.
# (change "3" to "2" if you use python2):
#
# .. code-block:: bash
#
#   pip3 install --user mxnet requests pillow
#
# Now return to the python code. Import packages.

from __future__ import absolute_import, print_function

import argparse, json, os, requests, time
from io import BytesIO
from os.path import join, isfile
from PIL import Image

from mxnet.gluon.model_zoo import vision
import numpy as np
from matplotlib import pyplot as plt

import tvm
from tvm import rpc, autotvm, relay
from tvm.contrib import graph_runtime, util, download
from tvm.contrib.debugger import debug_runtime

import vta
from vta.testing import simulator
from vta.top import graph_pack

# Make sure that TVM was compiled with RPC=1
assert tvm.module.enabled("rpc")


######################################################################
# Define the platform and model targets
# -------------------------------------
# Execute on CPU vs. VTA, and define the model.

# Load VTA parameters from the vta/config/vta_config.json file
env = vta.get_env()

# Set ``device=arm_cpu`` to run inference on the CPU
# or ``device=vta`` to run inference on the FPGA.
device = "vta"
target = env.target if device == "vta" else env.target_vta_cpu

# Name of Gluon model to compile
# The ``start_pack`` and ``stop_pack`` labels indicate where
# to start and end the graph packing relay pass: in other words
# where to start and finish offloading to VTA.
model = "resnet18_v1"
start_pack="nn.max_pool2d"
stop_pack="nn.global_avg_pool2d"

######################################################################
# Obtain an execution remote
# ---------------------------------
# When target is 'pynq', reconfigure FPGA and runtime.
# Otherwise, if target is 'sim', execute locally.

if env.TARGET != "sim":

    # Get remote from tracker node if environment variable is set.
    # To set up the tracker, you'll need to follow the "Auto-tuning
    # a convolutional network for VTA" tutorial.
    tracker_host = os.environ.get("TVM_TRACKER_HOST", None)
    tracker_port = int(os.environ.get("TVM_TRACKER_PORT", None))
    # Otherwise if you have a device you want to program directly from
    # the host, make sure you've set the variables below to the IP of
    # your board.
    device_host = os.environ.get("VTA_PYNQ_RPC_HOST", "192.168.2.99")
    device_port = int(os.environ.get("VTA_PYNQ_RPC_PORT", "9091"))
    if not tracker_host or not tracker_port:
        remote = rpc.connect(device_host, device_port)
    else:
        remote = autotvm.measure.request_remote(env.TARGET, tracker_host, tracker_port, timeout=10000)

    # Reconfigure the JIT runtime and FPGA.
    # You can program the FPGA with your own custom bitstream
    # by passing the path to the bitstream file instead of None.
    reconfig_start = time.time()
    vta.reconfig_runtime(remote)
    vta.program_fpga(remote, bitstream=None)
    reconfig_time = time.time() - reconfig_start
    print("Reconfigured FPGA and RPC runtime in {0:.2f}s!".format(reconfig_time))

# In simulation mode, host the RPC server locally.
else:
    remote = rpc.LocalSession()

# Get execution context from remote
ctx = remote.ext_dev(0) if device == "vta" else remote.cpu(0)

######################################################################
# Build the inference graph runtime
# ---------------------------------
# Grab ResNet-18 model from Gluon model zoo and compile with Relay.
# The compilation steps are:
#    1) Front end translation from MxNet into Relay module.
#    2) Apply 8-bit quantization: here we skip the first conv layer,
#       and dense layer which will both be executed in fp32 on the CPU.
#    3) Perform graph packing to alter the data layout for tensorization.
#    4) Perform constant folding to reduce number of operators (e.g. eliminate
#       batch norm multiply).
#    5) Perform relay build to object file.
#    6) Load the object file onto remote (FPGA device).
#    7) Generate graph runtime, `m`.

# Load pre-configured AutoTVM schedules
with autotvm.tophub.context(target):

    # Populate the shape and data type dictionary for ResNet input
    dtype_dict = {"data": 'float32'}
    shape_dict = {"data": (env.BATCH, 3, 224, 224)}

    # Get off the shelf gluon model, and convert to relay
    gluon_model = vision.get_model(model, pretrained=True)

    # Measure build start time
    build_start = time.time()

    # Start front end compilation
    mod, params = relay.frontend.from_mxnet(gluon_model, shape_dict)

    # Update shape and type dictionary
    shape_dict.update({k: v.shape for k, v in params.items()})
    dtype_dict.update({k: str(v.dtype) for k, v in params.items()})

    # Perform quantization in Relay
    with relay.quantize.qconfig(global_scale=8.0,
                                skip_conv_layers=[0]):
        relay_prog = relay.quantize.quantize(mod[mod.entry_func], params=params)

    # Perform graph packing and constant folding for VTA target
    if target.device_name == "vta":
        assert env.BLOCK_IN == env.BLOCK_OUT
        relay_prog = graph_pack(
            relay_prog,
            env.BATCH,
            env.BLOCK_OUT,
            env.WGT_WIDTH,
            start_name=start_pack,
            stop_name=stop_pack)
        relay_prog = relay.ir_pass.fold_constant(relay_prog)

    # Compile Relay program with AlterOpLayout disabled
    with relay.build_config(opt_level=3, disabled_pass={"AlterOpLayout"}):
        if target.device_name != "vta":
            graph, lib, params = relay.build(
                relay_prog, target=target,
                params=params, target_host=env.target_host)
        else:
            with vta.build_config():
                graph, lib, params = relay.build(
                    relay_prog, target=target,
                    params=params, target_host=env.target_host)

    # Measure Relay build time
    build_time = time.time() - build_start
    print(model + " inference graph built in {0:.2f}s!".format(build_time))

    # Send the inference library over to the remote RPC server
    temp = util.tempdir()
    lib.save(temp.relpath("graphlib.o"))
    remote.upload(temp.relpath("graphlib.o"))
    lib = remote.load_module("graphlib.o")

    # Graph runtime
    m = graph_runtime.create(graph, lib, ctx)

######################################################################
# Perform ResNet-18 inference
# ---------------------------
# We run classification on an image sample from ImageNet
# We just need to download the categories files, `synset.txt`
# and an input test image.

# Download ImageNet categories
categ_url = "https://github.com/uwsaml/web-data/raw/master/vta/models/"
categ_fn = "synset.txt"
download.download(join(categ_url, categ_fn), categ_fn)
synset = eval(open(categ_fn).read())

# Download test image
image_url = 'https://homes.cs.washington.edu/~moreau/media/vta/cat.jpg'
response = requests.get(image_url)

# Prepare test image for inference
image = Image.open(BytesIO(response.content)).resize((224, 224))
plt.imshow(image)
plt.show()
image = np.array(image) - np.array([123., 117., 104.])
image /= np.array([58.395, 57.12, 57.375])
image = image.transpose((2, 0, 1))
image = image[np.newaxis, :]
image = np.repeat(image, env.BATCH, axis=0)

# Set the network parameters and inputs
m.set_input(**params)
m.set_input('data', image)

# Perform inference: we run the module 4 times,
# and repeat 3 times to get error bounds
timer = m.module.time_evaluator("run", ctx, number=4, repeat=3)
tcost = timer()

# Get classification results
tvm_output = m.get_output(0, tvm.nd.empty((env.BATCH, 1000), "float32", remote.cpu(0)))
top_categories = np.argsort(tvm_output.asnumpy()[0])

# Report top-5 classification results
std = np.std(tcost.results) * 1000 / env.BATCH
mean = tcost.mean * 1000 / env.BATCH
print("%s prediction" % model)
print("                     #1:", synset[top_categories[-1]])
print("                     #2:", synset[top_categories[-2]])
print("                     #3:", synset[top_categories[-3]])
print("                     #4:", synset[top_categories[-4]])
print("                     #5:", synset[top_categories[-5]])
print("Performed inference in %.2fms/sample (std = %.2f)" % (mean, std))

# This just checks that one of the 5 top categories
# is one variety of cat; this is by no means an accurate
# assessment of how quantization affects classification
# accuracy but is meant to catch changes to the
# quantization pass that would accuracy in the CI.
cat_detected = False
for k in top_categories[-5:]:
    if "cat" in synset[k]:
        cat_detected = True
assert(cat_detected)
