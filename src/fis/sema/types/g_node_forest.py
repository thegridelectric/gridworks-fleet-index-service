from typing import Literal
from fis.sema.base import SemaType
from fis.sema.property_format import LeftRightDot
from fis.sema.property_format import UTCMilliseconds
from fis.sema.types.connectivity_edge_gt import ConnectivityEdgeGt
from fis.sema.types.g_node_gt import GNodeGt


class GNodeForest(SemaType):
    """Sema: https://schemas.electricity.works/types/g.node.forest/002"""

    roots: list[LeftRightDot]
    nodes: list[GNodeGt]
    edges: list[ConnectivityEdgeGt]
    send_time_ms: UTCMilliseconds
    proof: str | None = None
    type_name: Literal["g.node.forest"] = "g.node.forest"
    version: Literal["002"] = "002"
