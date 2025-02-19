import numpy as np
import ctypes
import extra.hip_wrapper as hip
from tinygrad.helpers import DEBUG
from tinygrad.ops import Compiled
from tinygrad.runtime.lib import RawBufferCopyInOut
from tinygrad.codegen.cstyle import CStyleCodegen, CStyleLanguage

# TODO: if you fork and exit the child process after creating anything with cl on AMD, it hangs on e.wait()
if DEBUG >= 5:
  from extra.helpers import enable_early_exec
  early_exec = enable_early_exec()

# The default HIP stream is used for everything.

class RawHIPBuffer(RawBufferCopyInOut):
  def __init__(self, size, dtype): super().__init__(size, dtype, hip.hipMalloc(size * dtype.itemsize))
  def __del__(self): hip.hipFree(self._buf)
  def _copyin(self, x:np.ndarray): hip.hipMemcpyAsync_htod(self._buf, x.ctypes.data, self.size * self.dtype.itemsize, 0)
  def _copyout(self, x:np.ndarray): hip.hipMemcpy_dtoh(x.ctypes.data, self._buf, self.size * self.dtype.itemsize)

class HIPProgram:
  def __init__(self, name:str, prg:str, binary=False):
    try:
      if not binary:
        prog = hip.hiprtcCreateProgram(prg, name, [], [])
        device_properties = hip.hipGetDeviceProperties(hip.hipGetDevice())
        hip.hiprtcCompileProgram(prog, [f'--offload-arch={device_properties.gcnArchName}'])
        prg = hip.hiprtcGetCode(prog)
    except Exception as e:
      if DEBUG >= 3: print("FAILED TO BUILD", prg)
      raise e
    if DEBUG >= 5:
      asm = early_exec((["/opt/rocm/llvm/bin/llvm-objdump", '-d', '-'], prg))
      print('\n'.join([x for x in asm.decode('utf-8').split("\n") if 's_code_end' not in x]))

    module = hip.hipModuleLoadData(prg)
    self.prg = hip.hipModuleGetFunction(module, name)

  def __call__(self, global_size, local_size, *args, wait=False):
    if wait:
      start, end = hip.hipEventCreate(), hip.hipEventCreate()
      hip.hipEventRecord(start)
    class PackageStruct(ctypes.Structure):
      _fields_ = [(f'field{idx}', ctypes.c_void_p) for idx in range(len(args))]
    struct = PackageStruct(*[data._buf for data in args])
    hip.hipModuleLaunchKernel(self.prg, global_size[0], global_size[1], global_size[2], local_size[0], local_size[1], local_size[2], 0, 0, struct)
    if wait:
      hip.hipEventRecord(end)
      hip.hipEventSynchronize(end)
      return hip.hipEventElapsedTime(start, end)*1e-3

class HIPCodegen(CStyleCodegen):
  lang = CStyleLanguage(
    kernel_prefix = "#include <hip/hip_common.h>\n#define INFINITY (__builtin_inff())\n#define NAN (__builtin_nanf(\"\"))" + """
__device__ float4 max(float4 x, float4 y) { return float4(max(x.x, y.x), max(x.y, y.y), max(x.z, y.z), max(x.w, y.w)); }
__device__ float4 pow(float x, float4 y) { return float4(pow(x, y.x), pow(x, y.y), pow(x, y.z), pow(x, y.w)); }
__device__ float4 pow(float4 x, float4 y) { return float4(pow(x.x, y.x), pow(x.y, y.y), pow(x.z, y.z), pow(x.w, y.w)); }
__device__ float4 log2(float4 x) { return float4(log2(x.x), log2(x.y), log2(x.z), log2(x.w)); }
__device__ float4 exp2(float4 x) { return float4(exp2(x.x), exp2(x.y), exp2(x.z), exp2(x.w)); }
__device__ float4 sin(float4 x) { return float4(sin(x.x), sin(x.y), sin(x.z), sin(x.w)); }
extern "C" __global__
    """,
    smem_prefix = "__shared__ ", barrier = "__syncthreads();", float4 = "make_float4", uses_vload=True,
    half_prekernel = "#include <hip/hip_fp16.h>\nusing half4 = HIP_vector_type<half, 4>;" + """
__device__ float vload_half(size_t offset, const half *p) { return (float)*(p + offset); }
__device__ float2 vload_half2(size_t offset, const half *p) { return make_float2((float)*(p + offset*2), (float)*(p + offset*2 + 1)); }
__device__ float4 vload_half4(size_t offset, const half *p) { return make_float4((float)*(p + offset*4), (float)*(p + offset*4 + 1), (float)*(p + offset*4 + 2), (float)*(p + offset*4 + 3)); }
__device__ void vstore_half(float data, size_t offset, half *p) { *(p + offset) = (half)data; }
__device__ void vstore_half2(float2 data, size_t offset, half *p) { *(p + offset*2) = (half)data.x; *(p + offset*2 + 1) = (half)data.y; }
__device__ void vstore_half4(float4 data, size_t offset, half *p) { *(p + offset*4) = (half)data.x; *(p + offset*4 + 1) = (half)data.y; *(p + offset*4 + 2) = (half)data.z; *(p + offset*4 + 3) = (half)data.w; }
    """,
    gid = [f'blockIdx.{chr(120+i)}' for i in range(3)],
    lid = [f'threadIdx.{chr(120+i)}' for i in range(3)])

HIPBuffer = Compiled(RawHIPBuffer, HIPCodegen, HIPProgram, hip.hipDeviceSynchronize)
