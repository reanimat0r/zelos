# Copyright (C) 2020 Zeropoint Dynamics

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public
# License along with this program.  If not, see
# <http://www.gnu.org/licenses/>.
# ======================================================================
import base64
import json
import logging
import os

from termcolor import colored

from zelos import CommandLineOption, IPlugin, Zelos


CommandLineOption(
    "snapshot", action="store_true", help="Output a snapshot of memory."
)


class Snapshotter(IPlugin):
    """
    Provides functionality for memory snapshots.
    """

    def __init__(self, z: Zelos):
        super().__init__(z)

        self.logger = logging.getLogger(__name__)

        if z.config.snapshot:
            if z.config.verbosity == 0:
                self.logger.error(
                    (
                        f"You will not get instruction comments without "
                        f'running in verbose mode. Include this flag ("-vv") '
                        f"if you want instruction comments in your snapshot. "
                        f"For an additional speedup, consider also including "
                        f'the fasttrace flag ("-vv --fasttrace").'
                    )
                )
            original_file_name = z.internal_engine.original_file_name

            def closure():
                with open(f"{original_file_name}.zmu", "w") as f:
                    self.snapshot(f)
                self.logger.info(
                    f"Wrote snaphot to: "
                    f"{os.path.abspath(original_file_name)}.zmu"
                )

            self.zelos.hook_close(closure)

    def _bad_section(self, data):
        # if size is too large, this is a bad section
        if len(data) > 0x100000000:
            self.logger.info(f"Data too large: 0x{len(data):x}")
            return True

        # if the data contains mostly zeros, we can ignore it
        def percent_zeros(data):
            num = 0
            for c in data:
                if c == 0:
                    num += 1
            return num / (1.0 * len(data))

        pct_zeros = percent_zeros(data)
        if pct_zeros > 0.999999:
            self.logger.info(f"Mostly zeros, pct: {pct_zeros}")
            return True

        return False

    def snapshot(self, outfile=None):
        """
        Dumps memory regions.

        Args:
            outfile: A file-like object to which output will be written. If
                not specified, snapshot will create a file with the name
                "memory_dump.zmu" to which output will be written.
        """
        z = self.zelos.internal_engine
        out_map = {}
        out_map["entrypoint"] = z.main_module.EntryPoint
        out_map["sections"] = []
        out_map["functions"] = []
        out_map["comments"] = []

        regions = self.emu.mem_regions()
        for region in sorted(regions):
            addr = region.address
            size = region.size
            perm = region.prot
            name = "<unk>"
            kind = "<unk>"
            if self.memory.get_region(addr) is not None:
                region = self.memory.get_region(addr)
                name = region.name
                kind = region.kind
            if addr == 0x80000000:
                continue  # GDT only
            dumped = False

            # Dump main binary
            if kind == "main" or name == "main":
                if "base_address" not in out_map:
                    out_map["base_address"] = addr
                section = {}
                section_name = name
                tmpname = name.split(" ")
                if len(tmpname) > 1:
                    section_name = tmpname[1]

                section["name"] = section_name
                section["address"] = addr
                section["permissions"] = perm
                # Temporary hack. The MEW packer requires executable
                # header section. But, we mark it non-executable for the
                # dump.
                if section_name == ".pe":
                    section["permissions"] = 0x1
                data = self.memory.read(addr, size)
                section["data"] = base64.b64encode(data).decode()
                dumped = True

            # Dump main and thread stacks binary
            if kind == "stack" and "dll_main" not in name:
                if "base_address" not in out_map:
                    out_map["base_address"] = addr
                section = {}
                section["name"] = "stack_" + name
                section["address"] = addr
                section["permissions"] = perm
                data = self.memory.read(addr, size)
                section["data"] = base64.b64encode(data).decode()
                dumped = True

            # Dump heap, sections, virtualalloc'd regions. Note that currently
            # we don't make use of dynamically allocated heaps, and so they are
            # excluded. Once that changes, we should include them here
            if (
                (kind == "heap" and name != "heap")
                or kind == "valloc"
                or kind == "section"
            ):
                if "base_address" not in out_map:
                    out_map["base_address"] = addr
                section = {}
                section["name"] = kind + "_" + name
                section["address"] = addr
                section["permissions"] = perm
                data = self.memory.read(addr, size)
                if kind == "heap" and name == "main_heap":
                    # Truncate unused portion of heap
                    section["data"] = base64.b64encode(
                        data[
                            : self.memory.heap.current_offset
                            - self.memory.HEAP_BASE
                        ]
                    ).decode()
                else:
                    section["data"] = base64.b64encode(data).decode()
                dumped = True

            line = (
                f"Region: 0x{addr:08x} Size: 0x{size:08x} "
                f"Perm: 0x{perm:x} \t{kind}\t\t{name}"
            )

            if dumped is True and self._bad_section(data):
                # Doppler cannot handle files that are this large at the
                # moment.
                dumped = False

            if dumped:
                print(colored(line, "white", attrs=["bold"]))
                out_map["sections"].append(section)
            else:
                print(line)

        for c in z.plugins.trace.comments:
            cmt = {}
            cmt["address"] = c.address
            cmt["thread_id"] = c.thread_id
            cmt["text"] = c.text
            out_map["comments"].append(cmt)

        for addr in z.plugins.trace.functions_called.keys():
            if addr < 0x10000000:
                function = {}
                function["address"] = addr
                function["name"] = "traced_{0:x}".format(addr)
                function["is_import"] = False
                out_map["functions"].append(function)

        r = json.dumps(out_map)
        loaded_r = json.loads(r)

        if outfile is None:
            with open("memory_dump.zmu", "w") as f:
                f.write(
                    "DISAS\n" + json.dumps(loaded_r, indent=4, sort_keys=True)
                )
        else:
            outfile.write(
                "DISAS\n" + json.dumps(loaded_r, indent=4, sort_keys=True)
            )