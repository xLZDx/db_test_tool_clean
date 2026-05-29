"""Lineage projection service.

Builds a graph-based lineage model where:
- Nodes represent columns at different stages (SOURCE, GENERATED, XML_STAGE, TARGET)
- Edges represent projections/transformations between stages

Supports tracing backward from any target column to its source expression.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from app.services.stage_alias_normalizer_service import (
    is_stage_qualifier,
    normalize_stage_column,
)
from app.services.sql_projection_parser_service import (
    extract_projection_edges,
    parse_select_projections,
)


class LineageNode:
    """A node in the lineage graph."""
    __slots__ = ("node_id", "node_type", "column", "qualifier")

    def __init__(self, node_id: str, node_type: str, column: str = "", qualifier: str = ""):
        self.node_id = node_id
        self.node_type = node_type
        self.column = column or node_id.rsplit(".", 1)[-1] if "." in node_id else node_id
        self.qualifier = qualifier

    def to_dict(self) -> Dict[str, str]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "column": self.column,
            "qualifier": self.qualifier,
        }


class LineageEdge:
    """An edge in the lineage graph."""
    __slots__ = ("from_node", "to_node", "edge_type")

    def __init__(self, from_node: str, to_node: str, edge_type: str):
        self.from_node = from_node
        self.to_node = to_node
        self.edge_type = edge_type

    def to_dict(self) -> Dict[str, str]:
        return {
            "from": self.from_node,
            "to": self.to_node,
            "edge_type": self.edge_type,
        }


class LineageGraph:
    """Graph representing column lineage across SQL stages."""

    def __init__(self):
        self.nodes: Dict[str, LineageNode] = {}
        self.edges: List[LineageEdge] = []
        self._reverse_index: Dict[str, List[LineageEdge]] = {}  # to_node → edges

    def add_node(self, node_id: str, node_type: str, column: str = "", qualifier: str = ""):
        if node_id not in self.nodes:
            self.nodes[node_id] = LineageNode(node_id, node_type, column, qualifier)

    def add_edge(self, from_node: str, to_node: str, edge_type: str):
        edge = LineageEdge(from_node, to_node, edge_type)
        self.edges.append(edge)
        self._reverse_index.setdefault(to_node, []).append(edge)

    def trace_back(self, target_node: str, max_depth: int = 10) -> List[str]:
        """Trace backward from a node to find all source nodes."""
        visited: Set[str] = set()
        sources: List[str] = []
        queue = [target_node]

        depth = 0
        while queue and depth < max_depth:
            next_queue = []
            for node in queue:
                if node in visited:
                    continue
                visited.add(node)
                incoming = self._reverse_index.get(node, [])
                if not incoming:
                    sources.append(node)
                else:
                    for edge in incoming:
                        next_queue.append(edge.from_node)
            queue = next_queue
            depth += 1

        return sources

    def get_root_source(self, target_column: str) -> Optional[str]:
        """Get the ultimate root source for a target column."""
        # Try exact match first
        target_node = f"TARGET.{target_column}"
        if target_node in self.nodes:
            sources = self.trace_back(target_node)
            return sources[0] if sources else None

        # Try finding any node that ends with the target column
        for node_id in self.nodes:
            if node_id.endswith(f".{target_column}") or node_id == target_column:
                sources = self.trace_back(node_id)
                if sources:
                    return sources[0]

        return None

    def get_output_column(self, target_column: str) -> Optional[str]:
        """Get the output column name for a target."""
        target_node = f"TARGET.{target_column}"
        node = self.nodes.get(target_node)
        return node.column if node else target_column

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "edges": [e.to_dict() for e in self.edges],
        }


def build_generated_lineage_graph(sql: str, target_columns: Optional[List[str]] = None) -> LineageGraph:
    """Build lineage graph from generated SQL.

    Parses SELECT projections and creates edges from source expressions to output aliases.
    """
    graph = LineageGraph()

    edges = extract_projection_edges(sql)
    for edge in edges:
        from_expr = edge["from_expression"]
        from_col = edge["from_column"]
        from_alias = edge["from_alias"]
        to_col = edge["to_column"]

        # Determine source node type
        if is_stage_qualifier(from_alias):
            source_type = "STAGE_COLUMN"
        else:
            source_type = "SOURCE_COLUMN"

        source_node_id = from_expr if from_alias else from_col
        output_node_id = f"GENERATED.{to_col}"

        graph.add_node(source_node_id, source_type, column=from_col, qualifier=from_alias)
        graph.add_node(output_node_id, "GENERATED_OUTPUT_COLUMN", column=to_col)
        graph.add_edge(source_node_id, output_node_id, "GENERATED_PROJECTION")

        # If target columns specified, link generated output to target
        if target_columns and to_col in [tc.upper() for tc in target_columns]:
            target_node_id = f"TARGET.{to_col}"
            graph.add_node(target_node_id, "TARGET_COLUMN", column=to_col)
            graph.add_edge(output_node_id, target_node_id, "OUTPUT_TO_TARGET")

    return graph


def build_xml_lineage_graph(
    xml_step_projections: List[Dict[str, Any]],
    target_columns: Optional[List[str]] = None,
) -> LineageGraph:
    """Build lineage graph from XML step projections.

    xml_step_projections: list of dicts like:
        {
            "step": "STEP5",
            "stage_table": "AVY_FACT_STEP5_STG_RT",
            "columns": [{"expression": "...", "output_alias": "..."}]
        }
    """
    graph = LineageGraph()

    for step_info in xml_step_projections:
        step_name = step_info.get("step", "")
        stage_table = step_info.get("stage_table", "")

        for col_info in step_info.get("columns", []):
            expr = col_info.get("expression", "").upper()
            alias = col_info.get("output_alias", "").upper()

            if not alias:
                continue

            source_node_id = f"{stage_table}.{alias}" if stage_table else expr
            output_node_id = f"XML_{step_name}.{alias}"

            graph.add_node(source_node_id, "XML_STAGE_COLUMN", column=alias, qualifier=stage_table)
            graph.add_node(output_node_id, "XML_OUTPUT_COLUMN", column=alias)
            graph.add_edge(source_node_id, output_node_id, "XML_STAGE_PROJECTION")

            # Link to target if applicable
            if target_columns and alias in [tc.upper() for tc in target_columns]:
                target_node_id = f"TARGET.{alias}"
                graph.add_node(target_node_id, "TARGET_COLUMN", column=alias)
                graph.add_edge(output_node_id, target_node_id, "XML_FINAL_PROJECTION")

    return graph


def compare_column_lineage(
    target_column: str,
    generated_graph: LineageGraph,
    xml_graph: Optional[LineageGraph],
    drd_source_attribute: str = "",
    pdm_cache: Optional[Dict[str, Any]] = None,
    saved_rules: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Compare lineage for a single target column across generated and XML graphs.

    Returns a match status string.
    """
    target_col = target_column.upper()

    # Get generated lineage
    gen_output_node = f"GENERATED.{target_col}"
    gen_sources = generated_graph.trace_back(gen_output_node) if gen_output_node in generated_graph.nodes else []

    if not xml_graph:
        # No XML — can only validate generated lineage
        if gen_sources:
            return "MATCH_BY_DRD_SOURCE_ATTRIBUTE"
        return "REVIEW_REQUIRED_LOW_CONFIDENCE"

    # Get XML lineage
    xml_sources = []
    for node_id in xml_graph.nodes:
        if node_id.endswith(f".{target_col}") and "XML_" in node_id:
            xml_sources = xml_graph.trace_back(node_id)
            break

    if not gen_sources and not xml_sources:
        return "REVIEW_REQUIRED_LOW_CONFIDENCE"

    # Compare output columns
    gen_output = generated_graph.nodes.get(gen_output_node)
    gen_output_col = gen_output.column if gen_output else ""

    # Find XML output for same target
    xml_output_col = ""
    for node_id, node in xml_graph.nodes.items():
        if node.column == target_col and node.node_type == "XML_OUTPUT_COLUMN":
            xml_output_col = node.column
            break

    # Rule A: Output alias match
    if gen_output_col and xml_output_col and gen_output_col == xml_output_col:
        return "MATCH_BY_OUTPUT_ALIAS"

    # Rule B: Root source match
    if gen_sources and xml_sources:
        gen_root_cols = {s.rsplit(".", 1)[-1] if "." in s else s for s in gen_sources}
        xml_root_cols = {s.rsplit(".", 1)[-1] if "." in s else s for s in xml_sources}
        if gen_root_cols & xml_root_cols:
            return "MATCH_BY_ROOT_SOURCE_LINEAGE"

    # Rule C: Stage projection match (both resolve to same target)
    if gen_output_col == target_col and xml_output_col == target_col:
        return "MATCH_BY_STAGE_PROJECTION"

    # Rule D: Saved rule
    if saved_rules:
        for rule in saved_rules:
            if rule.get("target_column", "").upper() == target_col and rule.get("decision") == "equivalent":
                return "MATCH_BY_SAVED_RULE"

    return "REAL_MISMATCH"
