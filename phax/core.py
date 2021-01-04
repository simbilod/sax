import functools

import jax
import jax.numpy as jnp

from .utils import zero, rename_ports, get_ports


def modelgenerator(ports, default_params=None, reciprocal=True):
    """function decorator to easily generate a model dictionary

    Args:
        ports: the port names of the model (port combination tuples will be the
            keys of the model dictionary)
        default_params: the dictionary containing the default model parameters.
        reciprocal: whether the model is reciprocal or not, i.e. whether
            model(i, j) == model(j, i).  If a model is reciprocal, the decorated
            model function only needs to be defined for i <= j.

    Returns:
        a decorator acting on a function-generating function.
    """
    ports = tuple(p for p in ports)
    num_ports = len(ports)

    def modeldecorator(modelgenerator):
        """generator a model dictionary from a function-generating function.

        Args:
            modelgenerator: the function-generating function taking two integer
                indices as arguments: (i, j).  modelgenerator(i, j) needs to return
                a function taking a single dictionary argument: the parameters of the
                function. modelgenerator(i, j) only needs to be defined for the nonzero
                elements.

        Returns:
            the model dictionary, for which each of the nonzero
            port-combinations is mapped to its corresponding model function.
        """
        m = {}
        m["default_params"] = {} if default_params is None else {**default_params}
        for j in range(num_ports):
            for i in range(j + 1):
                func = modelgenerator(i, j)
                if func is not None:
                    m[ports[i], ports[j]] = func
                if not reciprocal:
                    func = modelgenerator(j, i)
                if func is not None:
                    m[ports[j], ports[i]] = func
        return m

    return modeldecorator


def circuit(models, connections, ports):
    """create a (sub)circuit model from a collection of models and connections

    Args:
        models: a dictionary with as keys the model names and values
            the model dictionaries.
        connections: a dictionary where both keys and values are strings of the
            form "modelname:portname"
        ports: a dictionary mapping portnames of the form
            "modelname:portname" to new unique portnames

    Returns:
        the circuit model with the given port names.

    Example:
        A simple mzi can be created as follows::

            mzi = circuit(
                models = {
                    "left": model_directional_coupler,
                    "top": model_waveguide,
                    "bottom": model_waveguide,
                    "right": model_directional_coupler,
                },
                connections={
                    "left:p2": "top:in",
                    "left:p1": "bottom:in",
                    "top:out": "right:p3",
                    "bottom:out": "right:p0",
                },
                ports={
                    "left:p3": "in2",
                    "left:p0": "in1",
                    "right:p2": "out2",
                    "right:p1": "out1",
                },
            )
    """

    models, connections, ports = _validate_circuit_parameters(
        models, connections, ports
    )

    for name, model in models.items():
        models[name] = rename_ports(model, {p: f"{name}:{p}" for p in get_ports(model)})
    modelnames = [[name] for name in models]

    while len(modelnames) > 1:
        for names1, names2 in zip(modelnames[::2], modelnames[1::2]):
            model1 = models.pop(names1[0])
            model2 = models.pop(names2[0])
            model = _combine_models(
                model1,
                model2,
                None if len(names1) > 1 else names1[0],
                None if len(names2) > 1 else names2[0],
            )
            names1.extend(names2)
            conns = {}
            for port1, port2 in [(k, v) for k, v in connections.items()]:
                n1, p1 = port1.split(":")
                n2, p2 = port2.split(":")
                if n1 in names1 and n2 in names1:
                    del connections[port1]
                    model = _interconnect_model(model, port1, port2)
            models[names1[0]] = model
        modelnames = list(reversed(modelnames[::2]))

    model = rename_ports(model, ports)

    return model


def _validate_circuit_parameters(models, connections, ports):
    """validate the netlist parameters of a circuit

    Args:
        models: a dictionary with as keys the model names and values
            the model dictionaries.
        connections: a dictionary where both keys and values are strings of the
            form "modelname:portname"
        ports: a dictionary mapping portnames of the form
            "modelname:portname" to new unique portnames

    Returns:
        the validated and possibly slightly modified models, connections and
        ports dictionaries.
    """

    all_ports = set()
    for name, model in models.items():
        _validate_model_dict(name, model)
        for port in get_ports(model):
            all_ports.add(f"{name}:{port}")

    if not isinstance(connections, dict):
        msg = f"Connections should be a str:str dict or a list of length-2 tuples."
        assert all(len(conn) == 2 for conn in connections), msg
        connections, _connections = {}, connections
        connection_ports = set()
        for connection in _connections:
            connections[connection[0]] = connection[1]
            for port in connection:
                msg = f"Duplicate port found in connections: '{port}'"
                assert port not in connection_ports, msg
                connection_ports.add(port)

    connection_ports = set()
    for connection in connections.items():
        for port in connection:
            if port in all_ports:
                all_ports.remove(port)
            msg = f"Connection ports should all be strings. Got: '{port}'"
            assert isinstance(port, str), msg
            msg = f"Connection ports should have format 'modelname:port'. Got: '{port}'"
            assert len(port.split(":")) == 2, msg
            name, _port = port.split(":")
            msg = f"Model '{name}' used in connection "
            msg += f"'{connection[0]}':'{connection[1]}', "
            msg += f"but '{name}' not found in models dictionary."
            assert name in models, msg
            msg = f"Port name '{_port}' not found in model '{name}'. "
            msg += f"Allowed ports for '{name}': {get_ports(models[name])}"
            assert _port in get_ports(models[name]), msg
            msg = f"Duplicate port found in connections: '{port}'"
            assert port not in connection_ports, msg
            connection_ports.add(port)

    output_ports = set()
    for port, output_port in ports.items():
        if port in all_ports:
            all_ports.remove(port)
        msg = f"Ports keys in 'ports' should all be strings. Got: '{port}'"
        assert isinstance(port, str), msg
        msg = f"Port values in 'ports' should all be strings. Got: '{output_port}'"
        assert isinstance(output_port, str), msg
        msg = f"Port keys in 'ports' should have format 'model:port'. Got: '{port}'"
        assert len(port.split(":")) == 2, msg
        msg = f"Port values in 'ports' shouldn't contain a ':'. Got: '{output_port}'"
        assert ":" not in output_port, msg
        msg = f"Duplicate port found in ports or connections: '{port}'"
        assert port not in connection_ports, msg
        name, _port = port.split(":")
        msg = f"Model '{name}' used in output port "
        msg += f"'{port}':'{output_port}', "
        msg += f"but '{name}' not found in models dictionary."
        assert name in models, msg
        msg = f"Port name '{_port}' not found in model '{name}'. "
        msg += f"Allowed ports for '{name}': {get_ports(models[name])}"
        assert _port in get_ports(models[name]), msg
        connection_ports.add(port)
        msg = f"Duplicate port found in output ports: '{output_port}'"
        assert output_port not in output_ports, msg
        output_ports.add(output_port)

    assert not all_ports, f"Unused ports found: {all_ports}"

    return models, connections, ports


def _validate_model_dict(name, model):
    assert isinstance(model, dict), f"Model '{model}' should be a dictionary"
    ports = get_ports(model)
    assert ports, f"No ports in model {name}"
    for p1 in ports:
        for p2 in ports:
            msg = (
                f"model {name} port combination {p1}->{p2} is no function or callable."
            )
            assert callable(model.get((p1, p2), zero)), msg


def _namedparamsfunc(func, name, params):
    """make a model function look for its model name before acting on the parameters

    Args:
        func: the original model function acting on a dictionary of parameters
        name: the name of the model
        params: a dictionary for which the keys are the model names and the
            values are the model parameter dictionaries.
    """
    return func(params[name])


def _combine_models(model1, model2, name1=None, name2=None):
    """Combine two models into a combined model (without connecting any ports)

    Args:
        model1: the first model dictionary to combine
        model2: the second model dictionary to combine
        name1: the name of the first model (can be None for unnamed models)
        name2: the name of the second model (can be None for unnamed models)
    """
    model = {}
    model["default_params"] = {}
    for _model, _name in [(model1, name1), (model2, name2)]:
        for key, value in _model.items():
            try:
                p1, p2 = key
                if value is zero or _name is None:
                    model[p1, p2] = value
                else:
                    model[p1, p2] = _partialmodelfunc(_namedparamsfunc, value, _name)
                if _name is None:
                    model["default_params"].update(_model["default_params"])
                else:
                    model["default_params"][_name] = {**_model["default_params"]}
            except ValueError:
                if key != "default_params":
                    model[key] = value
    return model


def _interconnect_model(model, k, l):
    """interconnect two ports in a given model

    Args:
        model: the component for which to interconnect the given ports
        k: the first port name to connect
        l: the second port name to connect

    Returns:
        the resulting interconnected component, i.e. a component with two ports
        less than the original component.

    Note:
        The interconnect algorithm is based on equation 6 in the paper below::

          Filipsson, Gunnar. "A new general computer algorithm for S-matrix calculation
          of interconnected multiports." 11th European Microwave Conference. IEEE, 1981.
    """
    new_model = {}
    new_model["default_params"] = {**model["default_params"]}
    ports = get_ports(model)
    for i in ports:
        for j in ports:
            mij = model.get((i, j), zero)
            mik = model.get((i, k), zero)
            mil = model.get((i, l), zero)
            mkj = model.get((k, j), zero)
            mkk = model.get((k, k), zero)
            mkl = model.get((k, l), zero)
            mlj = model.get((l, j), zero)
            mlk = model.get((l, k), zero)
            mll = model.get((l, l), zero)
            if (
                (mij is zero)
                and ((mkj is zero) or (mil is zero))
                and ((mlj is zero) or (mik is zero))
                and ((mkj is zero) or (mll is zero) or (mik is zero))
                and ((mlj is zero) or (mkk is zero) or (mil is zero))
            ):
                continue
            new_model[i, j] = _partialmodelfunc(
                _model_ijkl, mij, mik, mil, mkj, mkk, mkl, mlj, mlk, mll
            )
    for key in list(new_model.keys()):
        try:
            i, j = key
            if i == k or i == l or j == k or j == l:
                del new_model[i, j]
        except ValueError:
            pass
    return new_model


def _model_ijkl(mij, mik, mil, mkj, mkk, mkl, mlj, mlk, mll, params):
    """combine the given model functions.

    Note:
        The interconnect algorithm is based on equation 6 in the paper below::

          Filipsson, Gunnar. "A new general computer algorithm for S-matrix calculation
          of interconnected multiports." 11th European Microwave Conference. IEEE, 1981.
    """
    mij = mij(params)
    mik = mik(params)
    mil = mil(params)
    mkj = mkj(params)
    mkk = mkk(params)
    mkl = mkl(params)
    mlj = mlj(params)
    mlk = mlk(params)
    mll = mll(params)
    return mij + (
        mkj * mil * (1 - mlk)
        + mlj * mik * (1 - mkl)
        + mkj * mll * mik
        + mlj * mkk * mil
    ) / ((1 - mkl) * (1 - mlk) - mkk * mll)


class _partialmodelfunc(functools.partial):
    """fun(params)

    Args:
        params: parameter dictionary for the model.

    Returns:
        Model transmission.
    """

    def __repr__(self):
        func = self.func
        while hasattr(func, "func"):
            func = func.func
        return repr(func)