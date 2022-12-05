import time
from lithops import Storage
import lithops
import subprocess as sp
from .alignment_mapper import AlignmentMapper
from ..parameters import PipelineParameters
import os
from .. import aux_functions as af

map1_cachefile = 'lithops_map1_checkpoint'
correction_cachefile = 'lithops_correction_checkpoint'
map2_cachefile = 'lithops_map2_checkpoint'

def index_correction_map(setname: str, bucket: str, exec_param: str, storage: Storage):
    """
    Corrects the index after the first map iteration. 
    All the set files must have the prefix "map_index_files/".
    Corrected indices will be stored with the prefix "corrected_index/".

    Args:
        setname (str): files to be corrected
        bucket (str): s3 bucket where the set is stored
        exec_param (str): string used to differentiate this pipeline execution from others with different parameters
        storage (Storage): s3 storage instance, generated by lithops
    """
    
    # Download all files related to this set
    filelist = storage.list_keys(bucket, f'map_index_files/{exec_param}/{setname}')
    for file in filelist:
        local_file = file.split("/")[-1]
        storage.download_file(bucket, file, '/tmp/' + local_file)

    # Execute correction scripts
    cmd = f'/function/bin/binary_reducer.sh /function/bin/merge_gem_alignment_metrics.sh 4 /tmp/{setname}* > /tmp/{setname}.intermediate.txt'
    sp.run(cmd, shell=True, check=True, universal_newlines=True)
    cmd2 = f'/function/bin/filter_merged_index.sh /tmp/{setname}.intermediate.txt /tmp/{setname}'
    sp.run(cmd2, shell=True, check=True, universal_newlines=True)

    # Upload corrected index to storage
    storage.upload_file('/tmp/' + setname + '.txt', bucket, f'corrected_index/{exec_param}/{setname}.txt')


def map(args: PipelineParameters, iterdata: list, map_func: AlignmentMapper, num_chunks: int) -> float:
    """
    Execute the map phase

    Args:
        args (PipelineParameters): pipeline arguments
        iterdata (list): iterdata generated in the preprocessing stage
        map_func (AlignmentMapper): class containing the map functions
        num_chunks (int): number of corrections needed

    Returns:
        float: time taken to execute this phase
    """
    
    ###################################################################
    #### START OF MAP/REDUCE
    ###################################################################
    log_level = "DEBUG"

    # Initizalize storage and backend instances
    storage = Storage()
    fexec = lithops.FunctionExecutor(log_level=log_level, runtime=args.runtime_id, runtime_memory=args.runtime_mem)

    if args.skip_map == "False":
        # Delete old files
        print("Deleting previous mapper outputs...")
        af.delete_files(storage, args, cloud_prefixes=[f'{args.file_format}/{args.execution_name}/'])

    print("Running Map Phase... " + str(len(iterdata)) + " functions")

    # Initizalize execution debug info
    start = time.time()

    if args.skip_map == "False":
        ###################################################################
        #### MAP: STAGE 1
        ###################################################################
        print("PROCESSING MAP: STAGE 1")

        # Load futures from previous execution
        map1_futures = af.load_cache(map1_cachefile, args)

        # Execute first map if futures were not found
        if (not map1_futures):
            map1_futures = fexec.map(map_func.map_alignment1, iterdata, timeout=int(args.func_timeout_map))

        # Get results either from the old futures or the new execution
        first_map_results = fexec.get_result(fs=map1_futures)

        # Dump futures into file
        af.dump_cache(map1_cachefile, map1_futures, args)

        ###################################################################
        #### MAP: GENERATE CORRECTED INDEXES
        ###################################################################
        print("PROCESSING INDEX CORRECTION")

        # Load futures from previous execution
        correction_futures = af.load_cache(correction_cachefile, args)

        # Execute correction if futures were not found
        if (not correction_futures):
            # Generate the iterdata for index correction
            index_iterdata = []
            for i in range(num_chunks):
                index_iterdata.append({'setname': args.fq_seqname + '_fq' + str(i + 1), 'bucket': str(args.bucket),
                                       'exec_param': args.execution_name})

            # Execute index correction
            correction_futures = fexec.map(index_correction_map, index_iterdata, timeout=int(args.func_timeout_map))

        # Get results either from the old futures or the new execution
        fexec.get_result(fs=correction_futures)

        # Dump futures into file
        af.dump_cache(correction_cachefile, correction_futures, args)

        ###################################################################
        #### MAP: STAGE 2
        ###################################################################
        print("PROCESSING MAP: STAGE 2")

        # Load futures from previous execution
        map2_futures = af.load_cache(map2_cachefile, args)

        # Execute correction if futures were not found
        if (not map2_futures):
            # Generate new iterdata
            newiterdata = []
            for worker in first_map_results:
                newiterdata.append({
                    'fasta_chunk': worker[0],
                    'fastq_chunk': worker[1],
                    'corrected_map_index_file': worker[2].split("-")[0] + ".txt",
                    'filtered_map_file': worker[3],
                    'base_name': worker[4],
                    'old_id': worker[5],
                    'exec_param': args.execution_name
                })

            # Execute second stage of map
            map2_futures = fexec.map(map_func.map_alignment2, newiterdata, timeout=int(args.func_timeout_map))

        # Get results either from the old futures or the new execution
        fexec.get_result(fs=map2_futures)

        # Dump futures into file
        af.dump_cache(map2_cachefile, map2_futures, args)

    else:  # Skip map and get keys from previous run
        print("skipping map phase and retrieving existing keys")
        storage.list_keys(args.bucket, prefix="csv/")

    # End of map
    end = time.time()
    map_time = end - start

    # Delete intermediate files
    af.delete_files(storage, args,
                    cloud_prefixes=[f'map_index_files/{args.execution_name}/',
                                    f'corrected_index/{args.execution_name}/',
                                    f'filtered_map_files/{args.execution_name}/'],
                    local_files=[f'/tmp/{args.execution_name}/{map1_cachefile}',
                                 f'/tmp/{args.execution_name}/{map2_cachefile}',
                                 f'/tmp/{args.execution_name}/{correction_cachefile}'])

    return map_time