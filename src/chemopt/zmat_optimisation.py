import inspect
import os
from datetime import datetime
from os.path import basename, join, normpath, splitext
from collections import deque

import numpy as np
import scipy.optimize

from cclib.parser.utils import convertor
from chemcoord.xyz_functions import to_molden
from chemopt.configuration import conf_defaults, fixed_defaults
from chemopt.interface.generic import calculate
from tabulate import tabulate


def optimise(zmolecule, symbols=None, md_out=None, el_calc_input=None,
             molden_out=None, opt_f=None, **kwargs):
    """Optimize a molecule.

    Args:
        zmolecule (chemcoord.Zmat):
        symbols (sympy expressions):

    Returns:
        list: A list of dictionaries. Each dictionary has three keys:
        ``['energy', 'grad_energy', 'zmolecule']``.
        The energy is given in Hartree
        The energy gradient ('grad_energy') is given in internal coordinates.
        The units are Hartree / Angstrom for bonds and
        Hartree / radians for angles and dihedrals.
        The :class:`~chemcoord.Zmat` instance given by ``zmolecule``
        contains the keys ``['energy', 'grad_energy']`` in ``.metadata``.
    """
    if opt_f is None:
        opt_f = scipy.optimize.minimize
    base = splitext(basename(inspect.stack()[-1][1]))[0]
    if md_out is None:
        md_out = '{}.md'.format(base)
    if molden_out is None:
        molden_out = '{}.molden'.format(base)
    if el_calc_input is None:
        el_calc_input = join('{}_el_calcs'.format(base),
                             '{}.inp'.format(base))
    for filepath in [md_out, molden_out, el_calc_input]:
        rename_existing(filepath)

    t1 = datetime.now()
    V = _get_V_function(zmolecule, el_calc_input, md_out, **kwargs)
    with open(md_out, 'w') as f:
        f.write(_get_header(zmolecule, start_time=_get_isostr(t1), **kwargs))

    energies, structures, grads_energy_C = [], [], deque([])
    grad_energy_X = None
    new_zm = zmolecule.copy()
    get_new_zm = _get_new_zm_f_generator(zmolecule)
    while not _is_converged(energies, grad_energy_X):
        new_zm = get_new_zm(grads_energy_C)
        energy, grad_energy_X, grad_energy_C = V(new_zm)
        new_zm.metadata['energy'] = energy
        structures.append(new_zm)
        energies.append(energy)
        grads_energy_C.popleft()
        grads_energy_C.append(grad_energy_C)

    to_molden([x['zmolecule'].get_cartesian() for x in calculated],
              buf=molden_out)
    t2 = datetime.now()
    with open(md_out, 'a') as f:
        footer = _get_footer(opt_zmat=structures[-1],
                             start_time=t1, end_time=t2,
                             molden_out=molden_out)
        f.write(footer)
    return calculated


def _get_V_function(zmolecule, el_calc_input, md_out, **kwargs):
    def V(zmolecule):
        result = calculate(molecule=zmolecule, forces=True,
                           el_calc_input=el_calc_input, **kwargs)
        energy = convertor(result.scfenergies[0], 'eV', 'hartree')
        grad_energy_X = result.grads[0] / convertor(1, 'bohr', 'Angstrom')

        grad_X = zmolecule.get_grad_cartesian(
            as_function=False, drop_auto_dummies=True)
        grad_energy_C = np.sum(
            grad_energy_X.T[:, :, None, None] * grad_X, axis=(0, 1))

        for i in range(min(3, grad_energy_C.shape[0])):
            grad_energy_C[i, i:] = 0.

        return energy, grad_energy_X, grad_energy_C
    return V


def _get_new_zm_f_generator(zmolecule):
    def get_new_zm(p, previous_zmat):
        C_deg = C_rad.copy().reshape((3, len(C_rad) // 3), order='F').T
        C_deg[:, [1, 2]] = np.rad2deg(C_deg[:, [1, 2]])

        new_zm = previous_zmat.copy()
        zmat_values = ['bond', 'angle', 'dihedral']
        new_zm.safe_loc[zmolecule.index, zmat_values] = C_deg
        return new_zm
    return get_new_zm


def _get_header(zmolecule, hamiltonian, basis, start_time, backend=None,
                charge=fixed_defaults['charge'], title=fixed_defaults['title'],
                multiplicity=fixed_defaults['multiplicity'], **kwargs):
    if backend is None:
        backend = conf_defaults['backend']
    get_header = """\
# This is ChemOpt {version} optimising a molecule in internal coordinates.

## Starting Structures
### Starting structure as Zmatrix

{zmat}

### Starting structure in cartesian coordinates

{cartesian}

## Setup for the electronic calculations
{electronic_calculation_setup}

## Iterations
Starting {start_time}

{table_header}
""".format

    def _get_table_header():
        get_row = '|{:>4.4}| {:^16.16} | {:^16.16} | {:^28.28} |'.format
        header = (get_row('n', 'energy [Hartree]',
                          'delta [Hartree]', 'grad_X_max [Hartree / Angstrom]')
                  + '\n'
                  + get_row(4 * '-', 16 * '-', 16 * '-', 28 * '-'))
        return header

    def _get_calc_setup(backend, hamiltonian, charge, multiplicity):
        data = [['Hamiltonian', hamiltonian],
                ['Charge', charge],
                ['Multiplicity', multiplicity]]
        return tabulate(data, tablefmt='pipe', headers=['Backend', backend])
    calculation_setup = _get_calc_setup(backend, hamiltonian, charge,
                                        multiplicity)

    header = get_header(
        version='0.1.0', title=title, zmat=_get_markdown(zmolecule),
        cartesian=_get_markdown(zmolecule.get_cartesian()),
        electronic_calculation_setup=calculation_setup,
        start_time=start_time,
        table_header=_get_table_header())
    return header


def _get_markdown(molecule):
    data = molecule._frame
    return tabulate(data, tablefmt='pipe', headers=data.columns)


def _get_table_row(calculated, grad_energy_X):
    n = len(calculated)
    energy = calculated[-1]['energy']
    if n == 1:
        delta = 0.
    else:
        delta = calculated[-1]['energy'] - calculated[-2]['energy']
    grad_energy_X_max = abs(grad_energy_X).max()
    get_str = '|{:>4}| {:16.10f} | {:16.10f} | {:28.10f} |\n'.format
    return get_str(n, energy, delta, grad_energy_X_max)


def rename_existing(filepath):
    if os.path.exists(filepath):
        to_be_moved = normpath(filepath).split(os.path.sep)[0]
        get_path = (to_be_moved + '_{}').format
        found = False
        end = 1
        while not found:
            if not os.path.exists(get_path(end)):
                found = True
            end += 1
        for i in range(end - 1, 1, -1):
            os.rename(get_path(i - 1), get_path(i))
        os.rename(to_be_moved, get_path(1))


def _get_footer(opt_zmat, start_time, end_time, molden_out):
    get_output = """\

## Optimised Structures
### Optimised structure as Zmatrix

{zmat}


### Optimised structure in cartesian coordinates

{cartesian}

## Closing

Structures were written to {molden}.

The calculation finished successfully at: {end_time}
and needed: {delta_time}.
""".format
    output = get_output(zmat=_get_markdown(opt_zmat),
                        cartesian=_get_markdown(opt_zmat.get_cartesian()),
                        molden=molden_out,
                        end_time=_get_isostr(end_time),
                        delta_time=str(end_time - start_time).split('.')[0])
    return output


def _get_isostr(time):
    return time.replace(microsecond=0).isoformat()


def _is_converged(energies, grad_energy_X, etol=1e-8, gtol=1e-5):
    """Returns if an optimization is converged.

    Args:
        energies (list): List of energies in hartree.
        grad_energy_X (numpy.ndarray): Gradient in cartesian coordinates
            hartree / Angstrom.
        etol (float): Tolerance for the energy.
        gtol (float): Tolerance for the maximum norm of the gradient.

    Returns:
        bool:
    """
    if len(energies) == 0:
        return False
    elif len(energies) == 1:
        return False
    else:
        return (abs(energies[-1] - energies[-2]) < etol and
                abs(grad_energy_X).max() < gtol)


def get_next_step(grads_energy_C):
    r"""Returns the next step in the BFGS algorithm.

    Args:
        grads_energy_C (collections.deque): A two element deque, that contains
            the current and previous gradient in internal coordinates.
            The order is: ``[previous, current]``.
            Each gradient is flatted out in the following order

            .. math::

                \left[
                    \frac{\partial V}{\partial r_1},
                    \frac{\partial V}{\partial \alpha_1},
                    \frac{\partial V}{\partial \delta_1},
                    \frac{\partial V}{\partial r_2},
                    \frac{\partial V}{\partial \alpha_2},
                    \frac{\partial V}{\partial \delta_2},
                    ...
                \right]

            Here :math:`V` is the energy and :math:`r_i, \alpha_i, \delta_i`
            are the bond, angle, and dihedral of the :math:`i`-th atom.
            The units are:

            .. math::

                    &\frac{\partial V}{\partial r_i}
                    &\frac{\text{Hartree}}{\text{Angstrom}}
                \\
                    &\frac{\partial V}{\partial \alpha_i}
                    &\frac{\text{Hartree}}{\text{Radian}}
                \\
                    &\frac{\partial V}{\partial \delta_i}
                    &\frac{\text{Hartree}}{\text{Radian}}


    Returns:
        chemcoord.Zmat:
    """
    # @Thorsten I assert this!
    if len(grads_energy_C) != 2:
        raise ValueError('Only deques of length 2 allowed')

    return next_step
