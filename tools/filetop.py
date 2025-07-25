#!/usr/bin/env python
# @lint-avoid-python-3-compatibility-imports
#
# filetop  file reads and writes by process.
#          For Linux, uses BCC, eBPF.
#
# USAGE: filetop.py [-h] [-a] [-C] [-r MAXROWS] [-p PID] [--read-only]
#                   [--write-only] [interval] [count]
#
# This uses in-kernel eBPF maps to store per process summaries for efficiency.
#
# Copyright 2016 Netflix, Inc.
# Licensed under the Apache License, Version 2.0 (the "License")
#
# 06-Feb-2016   Brendan Gregg   Created this.

from __future__ import print_function
from bcc import BPF
from time import sleep, strftime
import argparse
import os
import stat
from subprocess import call

# arguments
examples = """examples:
    ./filetop                 # file I/O top, 1 second refresh
    ./filetop -C              # don't clear the screen
    ./filetop -p 181          # PID 181 only
    ./filetop -d /home/user   # trace files in /home/user directory only
    ./filetop 5               # 5 second summaries
    ./filetop 5 10            # 5 second summaries, 10 times only
    ./filetop 5 --read-only   # 5 second summaries, only read operations traced
    ./filetop 5 --write-only  # 5 second summaries, only write operations traced
"""
parser = argparse.ArgumentParser(
    description="File reads and writes by process",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=examples)
parser.add_argument("-a", "--all-files", action="store_true",
    help="include non-regular file types (sockets, FIFOs, etc)")
parser.add_argument("-C", "--noclear", action="store_true",
    help="don't clear the screen")
parser.add_argument("-r", "--maxrows", default=20,
    help="maximum rows to print, default 20")
parser.add_argument("-s", "--sort", default="all",
    choices=["all", "reads", "writes", "rbytes", "wbytes"],
    help="sort column, default all")
parser.add_argument("-p", "--pid", type=int, metavar="PID", dest="tgid",
    help="trace this PID only")
parser.add_argument("--read-only", action="store_true",
    help="trace only reads")
parser.add_argument("--write-only", action="store_true",
    help="trace only writes")
parser.add_argument("interval", nargs="?", default=1,
    help="output interval, in seconds")
parser.add_argument("count", nargs="?", default=99999999,
    help="number of outputs")
parser.add_argument("--ebpf", action="store_true",
    help=argparse.SUPPRESS)
parser.add_argument("-d", "--directory", type=str,
    help="trace this directory only")

args = parser.parse_args()
interval = int(args.interval)
countdown = int(args.count)
maxrows = int(args.maxrows)
clear = not int(args.noclear)
debug = 0

# linux stats
loadavg = "/proc/loadavg"

# define BPF program
bpf_text = """
#include <uapi/linux/ptrace.h>
#include <linux/blkdev.h>

// the key for the output summary
struct info_t {
    unsigned long inode;
    dev_t dev;
    dev_t rdev;
    u32 pid;
    u32 name_len;
    char comm[TASK_COMM_LEN];
    // de->d_name.name may point to de->d_iname so limit len accordingly
    char name[DNAME_INLINE_LEN];
    char type;
};

// the value of the output summary
struct val_t {
    u64 reads;
    u64 writes;
    u64 rbytes;
    u64 wbytes;
};

BPF_HASH(counts, struct info_t, struct val_t);

static int do_entry(struct pt_regs *ctx, struct file *file,
    char __user *buf, size_t count, int is_read)
{
    u32 tgid = bpf_get_current_pid_tgid() >> 32;
    if (TGID_FILTER)
        return 0;

    u32 pid = bpf_get_current_pid_tgid();

    // skip I/O lacking a filename
    struct dentry *de = file->f_path.dentry;
    int mode = file->f_inode->i_mode;
    struct qstr d_name = de->d_name;
    if (d_name.len == 0 || TYPE_FILTER)
        return 0;

    // skip if not in the specified directory
    if (DIRECTORY_FILTER)
        return 0;

    // store counts and sizes by pid & file
    struct info_t info = {
        .pid = pid,
        .inode = file->f_inode->i_ino,
        .dev = file->f_inode->i_sb->s_dev,
        .rdev = file->f_inode->i_rdev,
    };
    bpf_get_current_comm(&info.comm, sizeof(info.comm));
    info.name_len = d_name.len;
    bpf_probe_read_kernel(&info.name, sizeof(info.name), d_name.name);
    if (S_ISREG(mode)) {
        info.type = 'R';
    } else if (S_ISSOCK(mode)) {
        info.type = 'S';
    } else {
        info.type = 'O';
    }

    struct val_t *valp, zero = {};
    valp = counts.lookup_or_try_init(&info, &zero);
    if (valp) {
        if (is_read) {
            valp->reads++;
            valp->rbytes += count;
        } else {
            valp->writes++;
            valp->wbytes += count;
        }
    }

    return 0;
}

int trace_read_entry(struct pt_regs *ctx, struct file *file,
    char __user *buf, size_t count)
{
    return do_entry(ctx, file, buf, count, 1);
}

int trace_write_entry(struct pt_regs *ctx, struct file *file,
    char __user *buf, size_t count)
{
    return do_entry(ctx, file, buf, count, 0);
}

"""
if args.tgid:
    bpf_text = bpf_text.replace('TGID_FILTER', 'tgid != %d' % args.tgid)
else:
    bpf_text = bpf_text.replace('TGID_FILTER', '0')
if args.all_files:
    bpf_text = bpf_text.replace('TYPE_FILTER', '0')
else:
    bpf_text = bpf_text.replace('TYPE_FILTER', '!S_ISREG(mode)')
if args.directory:
    try:
        directory_inode = os.lstat(args.directory)[stat.ST_INO]
        print(f'Tracing directory: {args.directory} (Inode: {directory_inode})')
        bpf_text = bpf_text.replace('DIRECTORY_FILTER',  'file->f_path.dentry->d_parent->d_inode->i_ino != %d' % directory_inode)
    except (FileNotFoundError, PermissionError) as e:
        print(f'Error accessing directory {args.directory}: {e}')
        exit(1)
else:
    bpf_text = bpf_text.replace('DIRECTORY_FILTER', '0')

if debug or args.ebpf:
    print(bpf_text)
    if args.ebpf:
        exit()

# initialize BPF
b = BPF(text=bpf_text)
if args.read_only and args.write_only:
    raise Exception("Both read-only and write-only flags passed")
elif args.read_only:
    b.attach_kprobe(event="vfs_read", fn_name="trace_read_entry")
elif args.write_only:
    b.attach_kprobe(event="vfs_write", fn_name="trace_write_entry")
else:
    b.attach_kprobe(event="vfs_read", fn_name="trace_read_entry")
    b.attach_kprobe(event="vfs_write", fn_name="trace_write_entry")
    

# check whether hash table batch ops is supported
htab_batch_ops = True if BPF.kernel_struct_has_field(b'bpf_map_ops',
        b'map_lookup_and_delete_batch') == 1 else False

DNAME_INLINE_LEN = 32  # linux/dcache.h

print('Tracing... Output every %d secs. Hit Ctrl-C to end' % interval)

def sort_fn(counts):
    if args.sort == "all":
        return (counts[1].rbytes + counts[1].wbytes + counts[1].reads + counts[1].writes)
    else:
        return getattr(counts[1], args.sort)

# output
exiting = 0
while 1:
    try:
        sleep(interval)
    except KeyboardInterrupt:
        exiting = 1

    # header
    if clear:
        call("clear")
    else:
        print()
    with open(loadavg) as stats:
        print("%-8s loadavg: %s" % (strftime("%H:%M:%S"), stats.read()))
    print("%-7s %-16s %-6s %-6s %-7s %-7s %1s %s" % ("TID", "COMM",
        "READS", "WRITES", "R_Kb", "W_Kb", "T", "FILE"))

    # by-TID output
    counts = b.get_table("counts")
    line = 0
    for k, v in reversed(sorted(counts.items_lookup_and_delete_batch()
                                if htab_batch_ops else counts.items(),
                                key=sort_fn)):
        name = k.name.decode('utf-8', 'replace')
        if k.name_len > DNAME_INLINE_LEN:
            name = name[:-3] + "..."

        # print line
        print("%-7d %-16s %-6d %-6d %-7d %-7d %1s %s" % (k.pid,
            k.comm.decode('utf-8', 'replace'), v.reads, v.writes,
            v.rbytes / 1024, v.wbytes / 1024,
            k.type.decode('utf-8', 'replace'), name))

        line += 1
        if line >= maxrows:
            break

    if not htab_batch_ops:
        counts.clear()

    countdown -= 1
    if exiting or countdown == 0:
        print("Detaching...")
        exit()
