'''
    Using multiprocessing for distributed running, 
    please specify the GPUs via CUDA_VISIBLE_DEVICES:
        CUDA_VISIBLE_DEVICES=0,1 python3 result.py --m 4096 --n 8192 --k 4096
'''

import torch
import json
from pathlib import Path
import torch.multiprocessing as mp
import pandas as pd
import argparse
import os

torch.ops.load_library("../build/lib/libst_pybinding.so")

WARM_UP=20
REP=200

def div_up(x: int, y: int):
    return (x + y - 1) // y

def reorder_indices(S, hint):
    # Generate the original array of indices [0, 1, ..., S-1]
    original = list(range(S))
    
    # Create an empty list to store the new order of indices
    new_order = [-1] * S
    
    # Place the indices of the hint list in the first positions of the new order
    for i, element in enumerate(hint):
        new_order[element] = i
    
    # Place the remaining indices in the new order
    remaining_elements = [x for x in original if x not in hint]
    for i, element in enumerate(remaining_elements, start=len(hint)):
        new_order[element] = i
    
    return torch.tensor(new_order, dtype=torch.int, device="cuda")

def perf_running_process(rank, world_size, nccl_id,
    M: int, N: int, K: int,
    BM: int, BN: int, Algo: int, cSeg: list, hint: list, 
    comm_op: str,
    result_dict):

    cSeg_CPU = torch.tensor(cSeg, dtype=torch.int32) 
    cSeg_GPU = cSeg_CPU.cuda(rank)

    TileNum = div_up(M, BM) * div_up(N, BN) 

    torch.cuda.set_device(rank)

    gemm_class = torch.classes.flashoverlap_class.OverlapImpl()

    gemm_class.nccl_init(rank, world_size, nccl_id)
    gemm_class.cutlass_init()
    gemm_class.overlap_init()

    A = torch.empty((M, K), dtype=torch.float16, device="cuda").normal_(mean=0., std=0.5)
    B = torch.empty((N, K), dtype=torch.float16, device="cuda").normal_(mean=0., std=0.5)
    C = torch.empty((M, N), dtype=torch.float16, device="cuda")

    MonitoredMatrix = torch.zeros(((N+BN-1)//BN), dtype=torch.int, device="cuda")
    ReorderedArray = reorder_indices(TileNum, hint).reshape(((M+BM-1)//BM, (N+BN-1)//BN))

    if len(cSeg) == 1:
        # No overlapping
        for _ in range(WARM_UP):
            gemm_class.gemm_allreduce(A, B, C, Algo)

        gemm_class.gemm_allreduce(A, B, C, Algo)

        start_event = [torch.cuda.Event(enable_timing=True) for i in range(REP)]
        end_event = [torch.cuda.Event(enable_timing=True) for i in range(REP)]
        for i in range(REP):
            start_event[i].record()
            gemm_class.gemm_allreduce(A, B, C, Algo)
            end_event[i].record()
        torch.cuda.synchronize()
        dur = torch.tensor([s.elapsed_time(e) for s, e in zip(start_event, end_event)], dtype=torch.float)

    else:
        for _ in range(WARM_UP):
            gemm_class.gemm_allreduce_overlap(A, B, C, MonitoredMatrix, ReorderedArray, 1, cSeg_CPU, cSeg_GPU, Algo, False)

        start_event = [torch.cuda.Event(enable_timing=True) for i in range(REP)]
        end_event = [torch.cuda.Event(enable_timing=True) for i in range(REP)]
        for i in range(REP):
            start_event[i].record()
            gemm_class.gemm_allreduce_overlap(A, B, C, MonitoredMatrix, ReorderedArray, 1, cSeg_CPU, cSeg_GPU, Algo, False)
            end_event[i].record()
        torch.cuda.synchronize()
        dur = torch.tensor([s.elapsed_time(e) for s, e in zip(start_event, end_event)], dtype=torch.float)

    result_dict[rank] = torch.mean(dur).item()
    
def perf_running(M: int, N: int, K: int, 
    BM: int, BN: int, Algo: int, 
    cSeg: list, hint: list, comm_op: str):
    world_size = torch.cuda.device_count()
    if world_size < 2:
        raise RuntimeError("At least 2 GPUs are required for this program.")

    nccl_id = torch.ops.flashoverlap_op.generate_nccl_id()
    torch.cuda.synchronize()
    # print(f"NCCL ID generated: {nccl_id[0]}")

    manager = mp.Manager()
    result_dict = manager.dict()

    mp.spawn(
            perf_running_process,
            args=(world_size, nccl_id, M, N, K, BM, BN, Algo, cSeg, hint, comm_op, result_dict),
            nprocs=world_size
        )

    dur = torch.empty((world_size))
    for i in range(world_size):
        dur[i] = result_dict[i]

    return dur.max()

# Function to initialize NCCL in each process
def perf_comm_process(rank, world_size, nccl_id, M, N, comm_type, result_dict):
    torch.cuda.set_device(rank)

    comm_class = torch.classes.flashoverlap_class.OverlapImpl()

    comm_class.nccl_init(rank, world_size, nccl_id)
    comm_class.cutlass_init()

    C = torch.empty((M, N), dtype=torch.float16, device="cuda").normal_(mean=0., std=0.5)

    for _ in range(WARM_UP):
        comm_class.nccl_allreduce(C)
    start_event = [torch.cuda.Event(enable_timing=True) for i in range(REP)]
    end_event = [torch.cuda.Event(enable_timing=True) for i in range(REP)]
    for i in range(REP):
        start_event[i].record()
        comm_class.nccl_allreduce(C)
        end_event[i].record()
    torch.cuda.synchronize()
    dur = torch.tensor([s.elapsed_time(e) for s, e in zip(start_event, end_event)], dtype=torch.float)
    
    result_dict[rank] = torch.mean(dur).item()

def perf_comm(M: int, N: int, comm_type: str):
    world_size = torch.cuda.device_count()
    if world_size < 2:
        raise RuntimeError("At least 2 GPUs are required!")
    
    nccl_id = torch.ops.flashoverlap_op.generate_nccl_id()
    torch.cuda.synchronize()
    # print(f"NCCL ID generated: {nccl_id[0]}")

    manager = mp.Manager()
    result_dict = manager.dict()

    # get the all reduce time
    mp.spawn(
            perf_comm_process,
            args=(world_size, nccl_id, M, N, comm_type, result_dict),
            nprocs=world_size
        )

    return result_dict[0]

# Function to initialize NCCL in each process
def perf_baseline_process(rank, world_size, nccl_id, M, N, K, comm_op, result_dict):
    torch.cuda.set_device(rank)

    A = torch.empty((M, K), dtype=torch.float16, device="cuda").normal_(mean=0., std=0.5)
    B = torch.empty((N, K), dtype=torch.float16, device="cuda").normal_(mean=0., std=0.5)
    C = torch.empty((M, N), dtype=torch.float16, device="cuda")

    # **** Init Baseline Class **** #
    gemm_comm = torch.classes.flashoverlap_class.BaselineImpl()
    gemm_comm.nccl_init(rank, world_size, nccl_id)
    gemm_comm.cublas_init()

    # **** cuBLAS + NCCL **** #
    for _ in range(WARM_UP):
        gemm_comm.gemm_allreduce(A, B, C)
    start_event = [torch.cuda.Event(enable_timing=True) for i in range(REP)]
    end_event = [torch.cuda.Event(enable_timing=True) for i in range(REP)]
    for i in range(REP):
        start_event[i].record()
        # torch.cuda.cudart().cudaProfilerStart()
        gemm_comm.gemm_allreduce(A, B, C)
        # torch.cuda.cudart().cudaProfilerStop()
        end_event[i].record()
    torch.cuda.synchronize()
    dur = torch.tensor([s.elapsed_time(e) for s, e in zip(start_event, end_event)], dtype=torch.float)
    
    result_dict[rank] = torch.mean(dur).item()

def perf_baseline(M: int, N: int, K: int, comm_op: str):
    world_size = torch.cuda.device_count()
    if world_size < 2:
        raise RuntimeError("At least 2 GPUs are required for this program.")
    # Use the custom NCCL initialization wrapper to get a unique NCCL ID
    # nccl_id = NcclInit()
    nccl_id = torch.ops.flashoverlap_op.generate_nccl_id()
    torch.cuda.synchronize()

    manager = mp.Manager()
    result_dict = manager.dict()

    # Spawn processes
    mp.spawn(
            perf_baseline_process,
            args=(world_size, nccl_id, M, N, K, comm_op, result_dict),
            nprocs=world_size
        )
    
    dur = torch.empty((world_size))
    for i in range(world_size):
        dur[i] = result_dict[i]
    
    return dur.max()

def main():
    device = torch.cuda.current_device()
    props = torch.cuda.get_device_properties(device)
    gpu_name = props.name[7:11].lower()
    sm_count = props.multi_processor_count
    wave_size = sm_count - 2

    parser = argparse.ArgumentParser()
    parser.add_argument('--m', type=int, default=4096)
    parser.add_argument('--k', type=int, default=8192)
    parser.add_argument('--n', type=int, default=8192)
    args = parser.parse_args()

    comm_op = 'all_reduce'

    m, n, k = args.m, args.n, args.k

    file_path = f'../configs/m{m}n{n}k{k}_{gpu_name}.json'

    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    tile_num = m // data["BM"] * n // data["BN"]
    wave_num = (tile_num + wave_size - 1) // wave_size

    gemm_dur = data["dur"]
    comm_dur = perf_comm(m, n, comm_op)
    overlap_dur = perf_running(m, n, k, 
        data["BM"], data["BN"], data["Algo"], data["cSeg"], data["hint"], comm_op)
    baseline_dur = perf_baseline(m, n, k, comm_op)

    speedup = baseline_dur / overlap_dur

    print(f"""
        {'Item':<10} {'Value':>15}
        {'-----':<10} {'-----':>15}
        {'m':<10} {m:>15}
        {'n':<10} {n:>15}
        {'k':<10} {k:>15}
        {'tile_num':<10} {tile_num:>15}
        {'gemm_dur (ms)':<10} {gemm_dur:>15.4f}
        {'comm_dur (ms)':<10} {comm_dur:>15.4f}
        {'baseline_dur (ms)':<10} {baseline_dur:>15.4f}
        {'overlap_dur (ms)':<10} {overlap_dur:>15.4f}
        {'speedup':<10} {speedup:>15.4f}
        """)

if __name__ == "__main__":
    main()




