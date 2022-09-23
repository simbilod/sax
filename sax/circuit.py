# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/07_circuit.ipynb (unless otherwise specified).


from __future__ import annotations


__all__ = ['create_dag', 'draw_dag', 'find_root', 'find_leaves', 'circuit', 'NetlistDict', 'CircuitInfo',
           'RecursiveNetlistDict']

# Cell
#nbdev_comment from __future__ import annotations

import os
import shutil
import sys
from functools import partial
from typing import Any, Dict, List, NamedTuple, Optional, Tuple, TypedDict, Union

import black
import networkx as nx
import numpy as np
from pydantic import ValidationError
from sax import reciprocal
from .backends import circuit_backends
from .multimode import multimode, singlemode
from .netlist import Netlist, RecursiveNetlist, load_recursive_netlist
from .typing_ import Model, Settings, SType
from .utils import _replace_kwargs, get_settings, merge_dicts, update_settings

# Cell
def create_dag(
    netlist: RecursiveNetlist,
    models: Optional[Dict[str, Any]] = None,
):
    if models is None:
        models = {}
    assert isinstance(models, dict)

    all_models = {}
    g = nx.DiGraph()

    for model_name, subnetlist in netlist.dict()['__root__'].items():
        if not model_name in all_models:
            all_models[model_name] = models.get(model_name, subnetlist)
            g.add_node(model_name)
        if model_name in models:
            continue
        for instance in subnetlist['instances'].values():
            component = instance['component']
            if not component in all_models:
                all_models[component] = models.get(component, None)
                g.add_node(component)
            g.add_edge(model_name, component)

    # we only need the nodes that depend on the parent...
    parent_node = next(iter(netlist.__root__.keys()))
    nodes = [parent_node, *nx.descendants(g, parent_node)]
    g = nx.induced_subgraph(g, nodes)

    return g

# Cell

def draw_dag(dag, with_labels=True, **kwargs):
    _patch_path()
    if shutil.which('dot'):
        return nx.draw(dag, nx.nx_pydot.pydot_layout(dag, prog='dot'), with_labels=with_labels, **kwargs)
    else:
        return nx.draw(dag, _my_dag_pos(dag), with_labels=with_labels, **kwargs)

def _patch_path():
    os_paths = {p: None for p in os.environ.get('PATH', '').split(os.pathsep)}
    sys_paths = {p: None for p in sys.path}
    other_paths = {os.path.dirname(sys.executable): None}
    os.environ['PATH'] = os.pathsep.join({**os_paths, **sys_paths, **other_paths})

def _my_dag_pos(dag):
    # inferior to pydot
    in_degree = {}
    for k, v in dag.in_degree():
        if v not in in_degree:
            in_degree[v] = []
        in_degree[v].append(k)

    widths = {k: len(vs) for k, vs in in_degree.items()}
    width = max(widths.values())
    height = max(widths) + 1

    horizontal_pos = {k: np.linspace(0, 1, w+2)[1:-1]*width for k, w in widths.items()}

    pos = {}
    for k, vs in in_degree.items():
        for x, v in zip(horizontal_pos[k], vs):
            pos[v] = (x, -k)
    return pos

# Cell
def find_root(g):
    nodes = [n for n, d in g.in_degree() if d == 0]
    return nodes

# Cell
def find_leaves(g):
    nodes = [n for n, d in g.out_degree() if d == 0]
    return nodes

# Cell
def _validate_models(models, dag):
    required_models = find_leaves(dag)
    missing_models = [m for m in required_models if m not in models]
    if missing_models:
        model_diff = {
            "Missing Models": missing_models,
            "Given Models": list(models),
            "Required Models": required_models,
        }
        raise ValueError(
            "Missing models. The following models are still missing to build the circuit:\n"
            f"{black.format_str(repr(model_diff), mode=black.Mode())}"
        )
    return {**models} # shallow copy

# Cell
def _flat_circuit(instances, connections, ports, models, backend):
    evaluate_circuit = circuit_backends[backend]

    inst2model = {k: models[inst.component] for k, inst in instances.items()}

    model_settings = {name: get_settings(model) for name, model in inst2model.items()}
    netlist_settings = {
        name: {k: v for k, v in (inst.settings or {}).items() if k in model_settings[name]}
        for name, inst in instances.items()
    }
    default_settings = merge_dicts(model_settings, netlist_settings)

    def _circuit(**settings: Settings) -> SType:
        settings = merge_dicts(default_settings, settings)
        settings = _forward_global_settings(inst2model, settings)

        instances: Dict[str, SType] = {}
        for inst_name, model in inst2model.items():
            instances[inst_name] = model(**settings.get(inst_name, {}))
        S = evaluate_circuit(instances, connections, ports)
        return S

    _replace_kwargs(_circuit, **default_settings)

    return _circuit

def _forward_global_settings(instances, settings):
    global_settings = {}
    for k in list(settings.keys()):
        if k in instances:
            continue
        global_settings[k] = settings.pop(k)
    if global_settings:
        settings = update_settings(settings, **global_settings)
    return settings

# Cell

def circuit(
    netlist: Union[Netlist, NetlistDict, RecursiveNetlist, RecursiveNetlistDict],
    models: Optional[Dict[str, Model]] = None,
    modes: Optional[List[str]] = None,
    backend: str = "default",
) -> Tuple[Model, CircuitInfo]:
    netlist, instance_models = _extract_instance_models(netlist) # TODO: do this *after* recursive netlist parsing.
    recnet: RecursiveNetlist = _validate_net(netlist)
    dependency_dag: nx.DiGraph = _validate_dag(create_dag(recnet, models))  # directed acyclic graph
    models = _validate_models({**(models or {}), **instance_models}, dependency_dag)
    modes = _validate_modes(modes)
    backend = _validate_circuit_backend(backend)

    circuit = None
    new_models = {}
    current_models = {}
    model_names = list(nx.topological_sort(dependency_dag))[::-1]
    for model_name in model_names:
        if model_name in models:
            new_models[model_name] = models[model_name]
            continue

        flatnet = recnet.__root__[model_name]

        connections, ports, new_models = _make_singlemode_or_multimode(
            flatnet, modes, new_models
        )
        current_models.update(new_models)
        new_models = {}

        current_models[model_name] = circuit = _flat_circuit(
            flatnet.instances, connections, ports, current_models, backend
        )

    assert circuit is not None
    return circuit, CircuitInfo(dag=dependency_dag, models=current_models)

class NetlistDict(TypedDict):
    instances: Dict
    connections: Dict[str, str]
    ports: Dict[str, str]

RecursiveNetlistDict = Dict[str, NetlistDict]

class CircuitInfo(NamedTuple):
    dag: nx.DiGraph
    models: Dict[str, Model]


def _extract_instance_models(netlist):
    if not isinstance(netlist, dict):
        netlist = netlist.dict()
    if '__root__' in netlist:
        netlist = netlist['__root__']
    if 'instances' in netlist:
        netlist = {'top_level': netlist}
    netlist = {**netlist}

    models = {}
    for netname, net in netlist.items():
        net = {**net}
        net['instances'] = {**net['instances']}
        for name, inst in net['instances'].items():
            if callable(inst):
                settings = get_settings(inst)
                if isinstance(inst, partial) and inst.args:
                    raise ValueError("SAX circuits and netlists don't support partials with positional arguments.")
                while isinstance(inst, partial):
                    inst = inst.func
                models[inst.__name__] = inst
                net['instances'][name] = {
                    'component': inst.__name__,
                    'settings': settings
                }
        netlist[netname] = net
    return netlist, models


def _validate_circuit_backend(backend):
    backend = backend.lower()
    # assert valid circuit_backend
    if backend not in circuit_backends:
        raise KeyError(
            f"circuit backend {backend} not found. Allowed circuit backends: "
            f"{', '.join(circuit_backends.keys())}."
        )
    return backend


def _validate_modes(modes) -> List[str]:
    if modes is None:
        return ["te"]
    elif not modes:
        return ["te"]
    elif isinstance(modes, str):
        return [modes]
    elif all(isinstance(m, str) for m in modes):
        return modes
    else:
        raise ValueError(f"Invalid modes given: {modes}")


def _validate_net(netlist: Union[Netlist, RecursiveNetlist]) -> RecursiveNetlist:
    if isinstance(netlist, dict):
        try:
            netlist = Netlist.parse_obj(netlist)
        except ValidationError:
            netlist = RecursiveNetlist.parse_obj(netlist)
    if isinstance(netlist, Netlist):
        netlist = RecursiveNetlist(__root__={"top_level": netlist})
    return netlist


def _validate_dag(dag):
    nodes = find_root(dag)
    if len(nodes) > 1:
        raise ValueError(f"Multiple top_levels found in netlist: {nodes}")
    if len(nodes) < 1:
        raise ValueError(f"Netlist does not contain any nodes.")
    if not dag.is_directed():
        raise ValueError("Netlist dependency cycles detected!")
    return dag


def _make_singlemode_or_multimode(netlist, modes, models):
    if len(modes) == 1:
        connections, ports, models = _make_singlemode(netlist, modes[0], models)
    else:
        connections, ports, models = _make_multimode(netlist, modes, models)
    return connections, ports, models


def _make_singlemode(netlist, mode, models):
    models = {k: singlemode(m, mode=mode) for k, m in models.items()}
    return netlist.connections, netlist.ports, models


def _make_multimode(netlist, modes, models):
    models = {k: multimode(m, modes=modes) for k, m in models.items()}
    connections = {
        f"{p1}@{mode}": f"{p2}@{mode}"
        for p1, p2 in netlist.connections.items()
        for mode in modes
    }
    ports = {
        f"{p1}@{mode}": f"{p2}@{mode}"
        for p1, p2 in netlist.ports.items()
        for mode in modes
    }
    return connections, ports, models