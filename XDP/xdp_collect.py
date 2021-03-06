#!/usr/bin/env python3

""" Work-in-Progress eBPF Flow Collecting Prototype """
# Requirements:
#   1. Network Driver of the following devices:
#       a. bnxt
#       b. thunder
#       c. i40e
#       d. ixgbe
#       e. mlx4/mlx5
#       f. nfp
#       g. qede
#       h. tun
#       i. virtio_net

# Imports #
import bcc
from bcc import BPF
import ctypes, struct
import argparse, logging
import os, sys, time
from socket import inet_ntoa, ntohl, ntohs
import ipaddress
from random import randint
from math import floor

# Global Variables #
CPU_COUNT = os.cpu_count()
key = 0
val = 1

def _sweep_flows(flows, cache, offset, agg_time):
    logging.info("Sweeping flows...")
    now = time.time()
    cur_flows = flows.items()
    cur_flows_len = len(cur_flows)
    old_flows = []

    # Loop through all recording flows
    for i in range(0, cur_flows_len):
        attrs = cur_flows[i][key]
        accms = cur_flows[i][val]  # accms[j] means "accumlators on cpu j"

        recent_stamp = 0
        # Loop through all CPUs and find most recent timestamp
        for j in range(0, CPU_COUNT):
            recent_stamp = accms[j].end if accms[j].end > recent_stamp else recent_stamp

        # Cache flow if exceeding aggregation time
        idle_time = now - (recent_stamp/1e9 + offset)
        if idle_time > agg_time:
            old_flows.append(attrs)
        
    # Final loop to cleanup "flows" hashmap and populate "cache"
    for flow in old_flows:
        cache.__setitem__(flow, flows.__getitem__(flow))
        flows.__delitem__(flow)
    logging.info(f"{len(old_flows)} flows swept")


# Main #
def main(args):
    with open('/proc/uptime') as inf:
        uptime = float(inf.read().split()[0])
    offset = time.time() - uptime

    # User argument handling
    loglevel = logging.INFO
    run_time = 5
    agg_time = 60
    out_file = "flows.csv"
    if args.debug:
        loglevel = logging.DEBUG
    if args.time:
        run_time = args.time
    if args.output:
        out_file = args.output
    if args.aggregate:
        agg_time = args.aggregate

    # Logger stuff
    logging.basicConfig(level=loglevel, 
                        format="[%(levelname)s] %(name)s : %(message)s")
    logger = logging.getLogger(__name__)

    # Retrieve user arguments
    IF = args.interface
    logger.info("Using interface %s" % IF)

    # CFlags for eBPF compile
    _cflags = []

    # Compile and load the required source C file
    logger.debug("Loading xdp_collect.c...")
    bpf = BPF(src_file="xdp_collect.c", \
              cflags=_cflags, \
              debug=bcc.DEBUG_SOURCE | bcc.DEBUG_BPF)

    # Get the main function
    logger.debug("Loading function xdp_parser()...")
    fn = bpf.load_func("xdp_parser", BPF.XDP)

    # Attach the flow collector
    logger.debug("Attaching xdp_parser() to kernel hook...")
    bpf.attach_xdp(IF, fn, 0)

    # Set up jump tables for protocol parsing
    for i, fn in [(4, "parse_ipv4"), (6, "parse_ipv6")]:
        _set_bpf_jumptable(bpf, "parse_layer3", i, fn, BPF.XDP)

    # 'py_start' holds epoch time since kernel startup, but only matters for while loop
    py_start = time.time()

    # Main flow collecting segment (Garbage Collector)
    logger.info("*** COLLECTING FOR %ss ***" % run_time)
    flows = bpf.get_table("flows")
    cache = bpf.get_table("cache")

    try:
        while abs(time.time() - py_start) < run_time:
            logger.debug("*** COLLECTING FOR %ss ***" % floor(run_time - (time.time() - py_start)))
            time.sleep(agg_time)
            _sweep_flows(flows, cache, offset, agg_time)
    except KeyboardInterrupt:
        logger.info("Caught ctrl+c; finishing")
        bpf.remove_xdp(IF, 0)
        
    # force sweeping all remaining flows
    _sweep_flows(flows, cache, offset, 0) 

    # Transfer remaining flows to cache
    logger.info("Caching ongoing flows")
    cur_flows = flows.items()
    len_cur_flows = len(cur_flows)
    for i in range(0, len_cur_flows):
        attrs = cur_flows[i][key]
        accms = cur_flows[i][val]
        cache.__setitem__(attrs, accms)

    try:
        # File to write to
        f = open(out_file, "w+")
        
        # Retrive individual items as list
        all_flows = cache.items()
        all_flows_len = len(all_flows)

        # This set will hold all flows to sort
        flow_set = set()

        # Writing to CSV + Debugging
        for i in range(0, all_flows_len):
            # Key: Attributes | Val: Accumulators
            attrs = all_flows[i][key]

            # Get accumulation variables over all CPUs
            n_packets = 0
            n_bytes = 0
            start = -1
            end = -1
            for j in range(0, CPU_COUNT):
                accms = all_flows[i][val][j]
                print("CPU {}: {}".format(j, accms.packets))
                n_packets += accms.packets
                n_bytes += accms.bytes
                if start == -1:
                    start = accms.start
                    end = accms.end
                else:
                    start = min(start, accms.start)
                    end = max(end, accms.end)
            start = start/1e9 + offset # convert to unix time
            end = end/1e9 + offset  # convert to unix time

            l2_proto = attrs.l2_proto
            l4_proto = attrs.l4_proto
            src_ip = ""
            dst_ip = ""

            # Parse IP addresses according to ethertype (EIP4 / EIP6)
            if l2_proto == 0x0800:
                src_ip = ipaddress.ip_address(ntohl(attrs.src_ip))
                dst_ip = ipaddress.ip_address(ntohl(attrs.dst_ip))
            elif l2_proto == 0x8100:
                src_ip = ipaddress.ipv6_address(ntohl(attrs.src_ip))
                dst_ip = ipaddress.ipv6_address(ntohl(attrs.dst_ip))

            # Port requires little endian conversion
            src_p = ntohs(attrs.src_port)
            dst_p = ntohs(attrs.dst_port)

            flow_set.add("{},{},{},{},{},{},{},{},{},{}\n"\
                      .format(start, end, src_ip, dst_ip, src_p, dst_p, hex(l2_proto), \
                              l4_proto, n_packets, n_bytes))
    finally:
        bpf.remove_xdp(IF, 0)
        f.close()
        logger.info("Removed XDP Program from Kernel.")

    # Final touch ups to CSV
    logger.info("Sorting CSV file...")
    s_flow_set = sorted(flow_set, key=lambda x:float(x[:x.find(',')]))
    f.close()

    f = open(out_file, "r+")
    f.write("START, END, SRC IP, DST IP, SRC PORT, DST PORT, ETHER TYPE, PROTO, #PACKETS, #BYTES\n")
    for entry in s_flow_set:
        f.write(entry)

    f.close()

    logger.info("Finished!")


# Credit to Joel Sommers
def _set_bpf_jumptable(bpf, tablename, idx, fnname, progtype):
    '''
    (bccobj, str, int, str, int) -> None
    Set up one entry in a bpf jump table to enable chaining
    bpf function calls.
    '''
    tail_fn = bpf.load_func(fnname, progtype)
    prog_array = bpf.get_table(tablename)
    prog_array[ctypes.c_int(idx)] = ctypes.c_int(tail_fn.fd)

# Argparse and Main Call #
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--interface", required=True, type=str,
                        help="Specify which interface to listen for packets on")
    parser.add_argument("-t", "--time", default=5, required=False, type=int, 
                        help="Time to run (s) [default = 5]")
    parser.add_argument("-a", "--aggregate", default=60, required=False, type=int,
                        help="Aggregation time (s) to close flows [default = 60]")
    parser.add_argument("-o", "--output", default="flows.csv", required=False, type=str,
                        help="Name of file to output flows in CSV format")
    parser.add_argument("-d", "--debug", default=False, required=False, action="store_true",
                        help="Allow debug logging")
    args = parser.parse_args()
    main(args)
