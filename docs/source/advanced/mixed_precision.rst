.. testsetup:: *

    from pytorch_lightning import Trainer


.. _amp:

Mixed Precision Training
========================

Mixed precision combines the use of both FP32 and lower bit floating points (such as FP16) to reduce memory footprint during model training, resulting in improved performance.

Lightning offers mixed precision training for GPUs and CPUs, as well as bfloat16 mixed precision training for TPUs.

.. note::

    In some cases it is important to remain in FP32 for numerical stability, so keep this in mind when using mixed precision.

    For example when running scatter operations during the forward (such as torchpoint3d) computation must remain in FP32.

FP16 Mixed Precision
--------------------

In most cases, mixed precision uses FP16. Supported torch operations are automatically run in FP16, saving memory and improving throughput on GPU and TPU accelerators.

Since computation happens in FP16, there is a chance of numerical instability. This is handled internally by a dynamic grad scaler which skips steps that are invalid, and adjusts the scaler to ensure subsequent steps fall within a finite range. For more information `see the autocast docs <https://pytorch.org/docs/stable/amp.html#gradient-scaling>`__.

.. note::

    When using TPUs, setting ``precision=16`` will enable bfloat16 which is the only supported precision type on TPUs.

.. testcode::
    :skipif: not _APEX_AVAILABLE and not _NATIVE_AMP_AVAILABLE or not torch.cuda.is_available()

    Trainer(gpus=1, precision=16)

BFloat16 Mixed Precision
------------------------

.. warning::

    BFloat16 requires PyTorch 1.10 or later. Currently this requires installing `PyTorch Nightly <https://pytorch.org/get-started/locally/>`__.

    BFloat16 is also experimental and may not provide large speedups or memory improvements, but offer better numerical stability.

    Do note for GPUs, largest benefits require `Ampere <https://en.wikipedia.org/wiki/Ampere_(microarchitecture)>`__ based GPUs, such as A100s or 3090s.

BFloat16 Mixed precision is similar to FP16 mixed precision, however we maintain more of the "dynamic range" that FP32 has to offer. This means we are able to improve numerical stability, compared to FP16 mixed precision. For more information see `this TPU performance blog post <https://cloud.google.com/blog/products/ai-machine-learning/bfloat16-the-secret-to-high-performance-on-cloud-tpus>`__.

Since BFloat16 is more stable than FP16 during training, we do not need to worry about any gradient scaling or nan gradient values that comes with using FP16 mixed precision.

.. testcode::
    :skipif: not _TORCH_BFLOAT_AVAILABLE

    Trainer(gpus=1, precision="bf16")

It is also possible to use BFloat16 mixed precision on the CPU, relying on MKLDNN under the hood.

.. testcode::
    :skipif: not _TORCH_CPU_AMP_AVAILABLE

    Trainer(precision="bf16")

NVIDIA APEX Mixed Precision
---------------------------

.. warning::

    We strongly recommend to use the above native mixed precision rather than NVIDIA APEX unless you require more finer control.

`NVIDIA APEX <https://github.com/NVIDIA/apex>`__ offers some additional flexibility in setting mixed precision. This can be useful for when wanting to try out different precision configurations, such as keeping most of your weights in FP16 as well as running computation in FP16.

.. testcode::
    :skipif: not _APEX_AVAILABLE and not _NATIVE_AMP_AVAILABLE or not torch.cuda.is_available()

    Trainer(gpus=1, amp_backend="apex")

Set the `NVIDIA optimization level <https://nvidia.github.io/apex/amp.html#opt-levels>`__ via the trainer.

.. testcode::
    :skipif: not _APEX_AVAILABLE and not _NATIVE_AMP_AVAILABLE or not torch.cuda.is_available()

    Trainer(gpus=1, amp_backend="apex", amp_level="O2")
