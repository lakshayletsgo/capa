#!/usr/bin/env python
"""
Copyright (C) 2023 Mandiant, Inc. All Rights Reserved.
Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
You may obtain a copy of the License at: [package root]/LICENSE.txt
Unless required by applicable law or agreed to in writing, software distributed under the License
 is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and limitations under the License.
"""
import io
import sys
import logging
import argparse
import contextlib

import capa.main
import capa.features.extractors.binexport2
from capa.features.extractors.binexport2.binexport2_pb2 import BinExport2

logger = logging.getLogger("inspect-binexport2")


class Renderer:
    def __init__(self, o: io.StringIO):
        self.o = o
        self.indent = 0

    @contextlib.contextmanager
    def indenting(self):
        self.indent += 1
        try:
            yield
        finally:
            self.indent -= 1

    def writeln(self, s):
        self.o.write("  " * self.indent)
        self.o.write(s)
        self.o.write("\n")

    @contextlib.contextmanager
    def section(self, name):
        self.writeln(name)
        with self.indenting():
            try:
                yield
            finally:
                pass
        self.writeln("/" + name)
        self.writeln("")

    def getvalue(self):
        return self.o.getvalue()


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(description="Inspect BinExport2 files")
    capa.main.install_common_args(parser, wanted={"input_file"})
    args = parser.parse_args(args=argv)

    try:
        capa.main.handle_common_args(args)
    except capa.main.ShouldExitError as e:
        return e.status_code

    o = Renderer(io.StringIO())
    be2: BinExport2 = capa.features.extractors.binexport2.get_binexport2(args.input_file)
    idx = capa.features.extractors.binexport2.BinExport2Index(be2)

    with o.section("meta"):
        o.writeln(f"name:   {be2.meta_information.executable_name}")
        o.writeln(f"sha256: {be2.meta_information.executable_id}")
        o.writeln(f"arch:   {be2.meta_information.architecture_name}")
        o.writeln(f"ts:     {be2.meta_information.timestamp}")

    with o.section("modules"):
        for module in be2.module:
            o.writeln(f"- {module.name}")
        if not be2.module:
            o.writeln("(none)")

    with o.section("sections"):
        for section in be2.section:
            perms = ""
            perms += "r" if section.flag_r else "-"
            perms += "w" if section.flag_w else "-"
            perms += "x" if section.flag_x else "-"
            o.writeln(f"- {hex(section.address)} {perms} {hex(section.size)}")

    with o.section("libraries"):
        for library in be2.library:
            o.writeln(
                f"- {library.name:<12s} {'(static)' if library.is_static else ''}{(' at ' + hex(library.load_address)) if library.HasField('load_address') else ''}"
            )
        if not be2.library:
            o.writeln("(none)")

    with o.section("functions"):
        for vertex_index, vertex in enumerate(be2.call_graph.vertex):
            if not vertex.HasField("address"):
                continue

            with o.section(f"function {idx.get_function_name_by_vertex(vertex_index)} @ {hex(vertex.address)}"):
                o.writeln(f"type:      {vertex.Type.Name(vertex.type)}")

                if vertex.HasField("mangled_name"):
                    o.writeln(f"name:      {vertex.mangled_name}")

                if vertex.HasField("demangled_name"):
                    o.writeln(f"demangled: {vertex.demangled_name}")

                if vertex.HasField("library_index"):
                    # TODO(williballenthin): this seems to be incorrect
                    library = be2.library[vertex.library_index]
                    o.writeln(f"library:   [{vertex.library_index}] {library.name}")

                if vertex.HasField("module_index"):
                    module = be2.module[vertex.module_index]
                    o.writeln(f"module:    [{vertex.module_index}] {module.name}")

                if idx.callees_by_vertex_index[vertex_index] or idx.callers_by_vertex_index[vertex_index]:
                    o.writeln("xrefs:")

                    for caller_index in idx.callers_by_vertex_index[vertex_index]:
                        o.writeln(f"  ← {idx.get_function_name_by_vertex(caller_index)}")

                    for callee_index in idx.callees_by_vertex_index[vertex_index]:
                        o.writeln(f"  → {idx.get_function_name_by_vertex(callee_index)}")

                if vertex.address not in idx.flow_graph_index_by_address:
                    o.writeln("(no flow graph)")
                else:
                    flow_graph_index = idx.flow_graph_index_by_address[vertex.address]
                    flow_graph = be2.flow_graph[flow_graph_index]

                    o.writeln("")
                    for basic_block_index in flow_graph.basic_block_index:
                        basic_block = be2.basic_block[basic_block_index]
                        basic_block_address = idx.basic_block_address_by_index[basic_block_index]

                        with o.section(f"basic block {hex(basic_block_address)}"):
                            for edge in idx.target_edges_by_basic_block_index[basic_block_index]:
                                if edge.type == BinExport2.FlowGraph.Edge.Type.CONDITION_FALSE:
                                    continue

                                source_basic_block_index = edge.source_basic_block_index
                                source_basic_block_address = idx.basic_block_address_by_index[source_basic_block_index]

                                o.writeln(
                                    f"↓ {BinExport2.FlowGraph.Edge.Type.Name(edge.type)} basic block {hex(source_basic_block_address)}"
                                )

                            for instruction_index in idx.instruction_indices(basic_block):
                                instruction = be2.instruction[instruction_index]
                                instruction_address = idx.instruction_address_by_index[instruction_index]

                                mnemonic = be2.mnemonic[instruction.mnemonic_index]

                                call_targets = ""
                                if instruction.call_target:
                                    call_targets = " "
                                    for call_target_address in instruction.call_target:
                                        call_target_name = idx.get_function_name_by_address(call_target_address)
                                        call_targets += f"→ function {call_target_name} @ {hex(call_target_address)} "

                                data_references = ""
                                if instruction_index in idx.data_reference_index_by_source_instruction_index:
                                    data_references = " "
                                    for data_reference_index in idx.data_reference_index_by_source_instruction_index[
                                        instruction_index
                                    ]:
                                        data_reference = be2.data_reference[data_reference_index]
                                        data_reference_address = data_reference.address
                                        data_references += f"⇥ data {hex(data_reference_address)} "

                                string_references = ""
                                if instruction_index in idx.string_reference_index_by_source_instruction_index:
                                    string_references = " "
                                    for (
                                        string_reference_index
                                    ) in idx.string_reference_index_by_source_instruction_index[instruction_index]:
                                        string_reference = be2.string_reference[string_reference_index]
                                        string_index = string_reference.string_table_index
                                        string = be2.string_table[string_index]
                                        string_references += f'⇥ string "{string}" '

                                comments = ""
                                if instruction.comment_index:
                                    comments = " "
                                    for comment_index in instruction.comment_index:
                                        comment = be2.comment[comment_index]
                                        comment_string = be2.string_table[comment.string_table_index]
                                        comments += f"; {BinExport2.Comment.Type.Name(comment.type)} {comment_string} "

                                o.writeln(
                                    f"{hex(instruction_address)}  {mnemonic.name:<12s}{call_targets}{data_references}{string_references}{comments}"
                                )

                            does_fallthrough = False
                            for edge in idx.source_edges_by_basic_block_index[basic_block_index]:
                                if edge.type == BinExport2.FlowGraph.Edge.Type.CONDITION_FALSE:
                                    does_fallthrough = True
                                    continue

                                back_edge = ""
                                if edge.HasField("is_back_edge") and edge.is_back_edge:
                                    back_edge = "↑"

                                target_basic_block_index = edge.target_basic_block_index
                                target_basic_block_address = idx.basic_block_address_by_index[target_basic_block_index]
                                o.writeln(
                                    f"→ {BinExport2.FlowGraph.Edge.Type.Name(edge.type)} basic block {hex(target_basic_block_address)} {back_edge}"
                                )

                            if does_fallthrough:
                                o.writeln("↓ CONDITION_FALSE")

    with o.section("data"):
        for data_address in sorted(idx.data_reference_index_by_target_address.keys()):
            if data_address in idx.instruction_index_by_address:
                # appears to be code
                continue

            data_references = ""
            for data_reference_index in idx.data_reference_index_by_target_address[data_address]:
                data_reference = be2.data_reference[data_reference_index]
                instruction_index = data_reference.instruction_index
                instruction_address = idx.instruction_address_by_index[instruction_index]
                data_references += f"⇤ {hex(instruction_address)} "
            o.writeln(f"{hex(data_address)} {data_references}")

    print(o.getvalue())


if __name__ == "__main__":
    sys.exit(main())
