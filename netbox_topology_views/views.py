import json
from typing import Optional, Collection, Iterable, Container, Dict, Mapping, Any, Set

from django.utils.html import escape
from django.conf import settings
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.db.models import Q
from django.http import HttpResponseRedirect, QueryDict
from django.shortcuts import render
from django.views.generic import View

from circuits.models import CircuitTermination, Circuit
from dcim.models.device_components import ComponentModel
from dcim.models import Device, Cable, DeviceRole
from extras.models import Tag

from .filters import DeviceFilterSet
from .forms import DeviceFilterForm


def _generate_description(title: str, from_data: Mapping[str, Any]) -> str:
    """
    Generate description if given supplementary data.
    :param title: Description title
    :param from_data: Description data
    :return:
    """
    title_tag = f'<span class="topology-item-description">{escape(title)}</span>'
    table_tag = (
        '<table class="topology-item-table"><tr>'
        + "</tr><tr>".join(
            f'<th align="right">{escape(title)}</th><td>{escape(content)}</td>'
            for title, content in from_data.items()
        )
        + "</tr></table>"
    )

    return title_tag + table_tag


def get_topology_data(
    queryset: Iterable[Device],
    hide_unconnected: bool = False,
    ignore_cable_types: Collection[str] = (),
    enable_circuit_terminations: Optional[bool] = True,
    enabled_device_images: Optional[Collection[str]] = None,
) -> Optional[Dict]:
    if not queryset:
        return None

    # Load plugin configuration
    plugin_config = settings.PLUGINS_CONFIG["netbox_topology_views"]

    if ignore_cable_types is None:
        ignore_cable_types: Collection[str] = plugin_config["ignore_cable_type"]

    if enable_circuit_terminations is None:
        enable_circuit_terminations: bool = plugin_config["enable_circuit_terminations"]

    if enabled_device_images is None:
        enabled_device_images: Container[str] = plugin_config["device_img"]

    nodes, edges = [], []

    nodes = []
    device_node_ids: Dict[int, int] = {}
    processed_circuits: Set[int] = set()

    def _reserve_device_node(device_id: int) -> int:
        """Reserve slot for arbitrary device node ID."""
        try:
            return device_node_ids[device_id]
        except LookupError:
            new_device_node_id = len(nodes)
            device_node_ids[device_id] = new_device_node_id

            # ignore issue; will be resolved at the last step
            # noinspection PyTypeChecker
            nodes.append(None)

            return new_device_node_id

    # Fetch cables for all selected device IDs
    device_ids = frozenset(d.id for d in queryset)
    cables_query = [
        Q(_termination_a_device_id__in=device_ids) | Q(_termination_b_device_id__in=device_ids),
    ]

    # Shave off circuit terminations, if not enabled
    if not enable_circuit_terminations:
        cables_query.append(
            ~(
                Q(termination_a_type__model="circuittermination")
                | Q(termination_b_type__model="circuittermination")
            )
        )

    # Shave off cable types, if provided
    if ignore_cable_types:
        cables_query.append(
            ~(
                Q(termination_a_type__name__in=ignore_cable_types)
                | Q(termination_b_type__name__in=ignore_cable_types)
            )
        )

    # Iterate over cables, add edges
    for cable in Cable.objects.filter(*cables_query):
        if (
            cable.termination_a_type in ignore_cable_types
            or cable.termination_b_type in ignore_cable_types
        ):
            continue

        edge = {"id": len(edges)}
        cable_data = {}
        title = None

        t_a, t_b = cable.termination_a, cable.termination_b
        if enable_circuit_terminations:

            def _get_device_bound_peer_termination(
                source_termination: CircuitTermination,
            ) -> Optional[ComponentModel]:
                """
                Get device for peer termination.
                :param source_termination:
                :return: Peer device, if exists
                """
                peer_termination = source_termination.get_peer_termination()
                if peer_termination is None or peer_termination.cable is None:
                    # Skip terminations which do not terminate, or do not have cables
                    return None

                # Get termination that contains the device
                device_termination = (
                    peer_termination.cable.termination_b
                    if peer_termination.cable.termination_a == peer_termination
                    else peer_termination.cable.termination_a
                )

                # @TODO: find less lazy approach to checking device existence
                if not hasattr(device_termination, "device") or device_termination.device is None:
                    # Other termination does not have a bound device
                    # @TODO: discover whether this is a possible scenario
                    return None

                return device_termination

            circuit: Optional[Circuit] = None
            if isinstance(t_a, CircuitTermination):
                # Process termination if it's on A side
                circuit = t_a.circuit
                if circuit.id in processed_circuits:
                    continue
                t_a = _get_device_bound_peer_termination(t_a)
                if t_a is None:
                    continue

            elif isinstance(t_b, CircuitTermination):
                # Process termination if it's on B side
                circuit = t_b.circuit
                if circuit.id in processed_circuits:
                    continue
                t_b = _get_device_bound_peer_termination(t_b)
                if t_b is None:
                    continue

            if circuit:
                # If any side is a termination, enrich data
                cable_data["Cable (From)"] = str(t_a.cable)
                if t_a.cable.type:
                    cable_data["Type (From)"] = t_a.cable.type

                cable_data["Cable (To)"] = str(t_b.cable)
                if t_b.cable.type:
                    cable_data["Type (To)"] = t_b.cable.type

                cable_data["Circuit"] = circuit
                cable_data["Provider"] = circuit.provider

                edge["dashes"] = True

                title = f"Circuit: {circuit}"

                # Save circuit ID so it doesn't get processed twice
                processed_circuits.add(circuit.id)

        else:
            if cable.type:
                cable_data["Type"] = cable.type

        if not (t_a.device.id in device_ids and t_b.device.id in device_ids):
            continue

        if title is None:
            title = f"Cable: {cable}"

        edge["from"] = _reserve_device_node(t_a.device.id)
        edge["to"] = _reserve_device_node(t_b.device.id)

        if cable.color != "":
            edge["color"] = f"#{cable.color}"

        # Prepend "To" and "From" before all else
        cable_data = {"To": f"{t_b.device} [{t_b}]", "From": f"{t_a.device} [{t_a}]", **cable_data}

        edge["title"] = _generate_description(title, cable_data)

        edges.append(edge)

    # Iterate over nodes
    for device in queryset:
        # Fetch reserved device node ID
        try:
            device_node_id = device_node_ids[device.id]
        except LookupError:
            if hide_unconnected:
                continue
            device_node_id = _reserve_device_node(device.id)

        # Generate node container
        device_label = str(device)
        role_image_slug = (
            device.device_role.slug
            if device.device_role.slug in enabled_device_images
            else "role-unknown"
        )
        node = {
            "id": device_node_id,
            "name": escape(
                device.name if device.name is None else f"untitled-{device.identifier}"
            ),
            "label": escape(device_label),
            "shape": "image",
            "image": f"../../static/netbox_topology_views/img/{role_image_slug}.png",
        }

        # Generate auxiliary data for device
        device_data = {"Status": device.status}
        if device.device_type:
            device_data["Type"] = device.device_type.model
        if device.device_role.name:
            device_data["Role"] = device.device_role.name
        if device.serial:
            device_data["Serial"] = device.serial
        if device.primary_ip:
            device_data["Primary IP"] = device.primary_ip

        # Generate title for device
        node["title"] = _generate_description(device_label, device_data)

        # Check if device role has a color
        if device.device_role.color:
            node["color.border"] = "#" + device.device_role.color

        # Create / update node
        nodes[device_node_id] = node

    return {"nodes": nodes, "edges": edges}


class TopologyHomeView(PermissionRequiredMixin, View):
    permission_required = ("dcim.view_site", "dcim.view_device")

    """
    Show the home page
    """

    def get(self, request):
        self.filterset = DeviceFilterSet
        self.queryset = Device.objects.all()
        self.queryset = self.filterset(request.GET, self.queryset).qs
        topo_data = None

        if request.GET:
            hide_unconnected = None
            if "hide_unconnected" in request.GET:
                if request.GET["hide_unconnected"] == "on":
                    hide_unconnected = True

            if "draw_init" in request.GET:
                if request.GET["draw_init"].lower() == "true":
                    topo_data = get_topology_data(self.queryset, hide_unconnected)
            else:
                topo_data = get_topology_data(self.queryset, hide_unconnected)
        else:
            preselected_device_roles = settings.PLUGINS_CONFIG["netbox_topology_views"][
                "preselected_device_roles"
            ]
            preselected_tags = settings.PLUGINS_CONFIG["netbox_topology_views"]["preselected_tags"]

            q_device_role_id = DeviceRole.objects.filter(
                name__in=preselected_device_roles
            ).values_list("id", flat=True)
            q_tags = Tag.objects.filter(name__in=preselected_tags).values_list("name", flat=True)

            q = QueryDict(mutable=True)
            q.setlist("device_role_id", list(q_device_role_id))
            q.setlist("tag", list(q_tags))
            q["draw_init"] = settings.PLUGINS_CONFIG["netbox_topology_views"][
                "draw_default_layout"
            ]
            query_string = q.urlencode()
            return HttpResponseRedirect(request.path + "?" + query_string)

        return render(
            request,
            "netbox_topology_views/index.html",
            {
                "filter_form": DeviceFilterForm(request.GET, label_suffix=""),
                "topology_data": json.dumps(topo_data),
            },
        )
