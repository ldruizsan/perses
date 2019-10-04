"""
This contains the base class for the geometry engine, which proposes new positions
for each additional atom that must be added.
"""
from simtk import unit

import numpy as np
import collections
import functools
import networkx as nx

from perses.storage import NetCDFStorage, NetCDFStorageView

################################################################################
# Initialize logging
################################################################################

import logging
logging.basicConfig(level = logging.NOTSET)
_logger = logging.getLogger("geometry")
_logger.setLevel(logging.DEBUG)



################################################################################
# Constants
################################################################################

LOG_ZERO = -1.0e+6
ENERGY_MISMATCH_RATIO_THRESHOLD = 1e-3
ENERGY_THRESHOLD = 1e-6

################################################################################
# Utility methods
################################################################################

def check_dimensionality(quantity, compatible_units):
    """
    Ensure that the specified quantity has units compatible with specified unit.

    Parameters
    ----------
    quantity : simtk.unit.Quantity or float
        The quantity to be checked
    compatible_units : simtk.unit.Quantity or simtk.unit.Unit or float
        Ensure ``quantity`` is either float or numpy array (if ``float`` specified) or is compatible with the specified units

    Raises
    ------
    ValueError if the specified quantity does not have the appropriate dimensionality or type

    Returns
    -------
    is_compatible : bool
        Returns True if dimensionality is as requested

    """
    if unit.is_quantity(compatible_units) or unit.is_unit(compatible_units):
        from simtk.unit.quantity import is_dimensionless
        if not is_dimensionless(quantity / compatible_units):
            raise ValueError('{} does not have units compatible with expected {}'.format(quantity, compatible_units))
    elif compatible_units == float:
        if not (isinstance(quantity, float) or isinstance(quantity, np.ndarray)):
            raise ValueError("'{}' expected to be a float, but was instead {}".format(quantity, type(quantity)))
    else:
        raise ValueError("Don't know how to handle compatible_units of {}".format(compatible_units))

    # Units are compatible if they pass this point
    return True

class GeometryEngine(object):
    """
    This is the base class for the geometry engine.

    Arguments
    ---------
    metadata : dict
        GeometryEngine-related metadata as a dict
    """

    def __init__(self, metadata=None, storage=None):
        # TODO: Either this base constructor should be called by subclasses, or we should remove its arguments.
        pass

    def propose(self, top_proposal, current_positions, beta):
        """
        Make a geometry proposal for the appropriate atoms.

        Arguments
        ----------
        top_proposal : TopologyProposal object
            Object containing the relevant results of a topology proposal
        beta : float
            The inverse temperature

        Returns
        -------
        new_positions : [n, 3] ndarray
            The new positions of the system
        """
        return np.array([0.0,0.0,0.0])

    def logp_reverse(self, top_proposal, new_coordinates, old_coordinates, beta):
        """
        Calculate the logp for the given geometry proposal

        Arguments
        ----------
        top_proposal : TopologyProposal object
            Object containing the relevant results of a topology proposal
        new_coordinates : [n, 3] np.ndarray
            The coordinates of the system after the proposal
        old_coordiantes : [n, 3] np.ndarray
            The coordinates of the system before the proposal
        direction : str, either 'forward' or 'reverse'
            whether the transformation is for the forward NCMC move or the reverse
        beta : float
            The inverse temperature

        Returns
        -------
        logp : float
            The log probability of the proposal for the given transformation
        """
        return 0.0


class FFAllAngleGeometryEngine(GeometryEngine):
    """
    This is an implementation of GeometryEngine which uses all valence terms and OpenMM

    Parameters
    ----------
    use_sterics : bool, optional, default=False
        If True, sterics will be used in proposals to minimize clashes.
        This may significantly slow down the simulation, however.
    n_bond_divisions : int, default 1000
        number of bond divisions in choosing the r for added/deleted atoms
    n_angle_divisions : int, default 180
        number of bond angle divisions in choosing theta for added/deleted atoms
    n_torsion_divisions : int, default 360
        number of torsion angle divisons in choosing phi for added/deleted atoms
    verbose: bool, default True
        whether to be verbose in output
    storage: bool (or None), default None
        whether to use NetCDFStorage
    bond_softening_constant : float (between 0, 1), default 1.0
        how much to soften bonds
    angle_softening_constant : float (between 0, 1), default 1.0
        how much to soften angles
    neglect_angles : bool, optional, default True
        whether to ignore and report on theta angle potentials that add variance to the work
    use_14_nonbondeds : bool, default True
        whether to consider 1,4 exception interactions in the geometry proposal
        NOTE: if this is set to true, then in the HybridTopologyFactory, the argument 'interpolate_old_and_new_14s' must be set to False; visa versa

    """
    def __init__(self,
                 metadata=None,
                 use_sterics=False,
                 n_bond_divisions=1000,
                 n_angle_divisions=180,
                 n_torsion_divisions=360,
                 verbose=True,
                 storage=None,
                 bond_softening_constant=1.0,
                 angle_softening_constant=1.0,
                 neglect_angles = False,
                 use_14_nonbondeds = True):
        self._metadata = metadata
        self.write_proposal_pdb = False # if True, will write PDB for sequential atom placements
        self.pdb_filename_prefix = 'geometry-proposal' # PDB file prefix for writing sequential atom placements
        self.nproposed = 0 # number of times self.propose() has been called
        self.verbose = verbose
        self.use_sterics = use_sterics
        self._use_14_nonbondeds = use_14_nonbondeds

        # if self.use_sterics: #not currently supported
        #     raise Exception("steric contributions are not currently supported.")


        self._n_bond_divisions = n_bond_divisions
        self._n_angle_divisions = n_angle_divisions
        self._n_torsion_divisions = n_torsion_divisions
        self._bond_softening_constant = bond_softening_constant
        self._angle_softening_constant = angle_softening_constant
        if storage:
            self._storage = NetCDFStorageView(modname="GeometryEngine", storage=storage)
        else:
            self._storage = None
        self.neglect_angles = neglect_angles

    def propose(self, top_proposal, current_positions, beta):
        """
        Make a geometry proposal for the appropriate atoms.

        Arguments
        ----------
        top_proposal : TopologyProposal object
            Object containing the relevant results of a topology proposal
        current_positions : simtk.unit.Quantity with shape (n_atoms, 3) with units compatible with nanometers
            The current positions
        beta : simtk.unit.Quantity with units compatible with 1/(kilojoules_per_mole)
            The inverse thermal energy

        Returns
        -------
        new_positions : [n, 3] ndarray
            The new positions of the system
        logp_proposal : float
            The log probability of the forward-only proposal
        """
        _logger.info("propose: performing forward proposal")
        # Ensure positions have units compatible with nanometers
        check_dimensionality(current_positions, unit.nanometers)
        check_dimensionality(beta, unit.kilojoules_per_mole**(-1))

        # TODO: Change this to use md_unit_system instead of hard-coding nanometers
        if not top_proposal.unique_new_atoms:
            _logger.info("propose: there are no unique new atoms; logp_proposal = 0.0.")
            # If there are no unique new atoms, return new positions in correct order for new topology object and log probability of zero
            # TODO: Carefully check this
            import parmed
            structure = parmed.openmm.load_topology(top_proposal.old_topology, top_proposal._old_system)
            atoms_with_positions = [ structure.atoms[atom_idx] for atom_idx in top_proposal.new_to_old_atom_map.keys() ]
            new_positions = self._copy_positions(atoms_with_positions, top_proposal, current_positions)
            logp_proposal, rjmc_info, atoms_with_positions_reduced_potential, final_context_reduced_potential, neglected_angle_terms = 0.0, None, None, None, None
            self.forward_final_growth_system = None
            self.forward_special_terms = None
        else:
            _logger.info("propose: unique new atoms detected; proceeding to _logp_propose...")
            logp_proposal, new_positions, rjmc_info, atoms_with_positions_reduced_potential, final_context_reduced_potential, neglected_angle_terms, special_terms = self._logp_propose(top_proposal, current_positions, beta, direction='forward')
            self.nproposed += 1
            self.forward_special_terms = special_terms

        check_dimensionality(new_positions, unit.nanometers)
        check_dimensionality(logp_proposal, float)

        #define forward attributes
        self.forward_rjmc_info = rjmc_info
        self.forward_atoms_with_positions_reduced_potential, self.forward_final_context_reduced_potential = atoms_with_positions_reduced_potential, final_context_reduced_potential
        self.forward_neglected_angle_terms = neglected_angle_terms

        return new_positions, logp_proposal


    def logp_reverse(self, top_proposal, new_coordinates, old_coordinates, beta):
        """
        Calculate the logp for the given geometry proposal

        Arguments
        ----------
        top_proposal : TopologyProposal object
            Object containing the relevant results of a topology proposal
        new_coordinates : simtk.unit.Quantity with shape (n_atoms, 3) with units compatible with nanometers
            The coordinates of the system after the proposal
        old_coordiantes : simtk.unit.Quantity with shape (n_atoms, 3) with units compatible with nanometers
            The coordinates of the system before the proposal
        beta : simtk.unit.Quantity with units compatible with 1/(kilojoules_per_mole)
            The inverse thermal energy

        Returns
        -------
        logp : float
            The log probability of the proposal for the given transformation
        """
        _logger.info("logp_reverse: performing reverse proposal")
        check_dimensionality(new_coordinates, unit.nanometers)
        check_dimensionality(old_coordinates, unit.nanometers)
        check_dimensionality(beta, unit.kilojoules_per_mole**(-1))

        # If there are no unique old atoms, the log probability is zero.
        if not top_proposal.unique_old_atoms:
            _logger.info("logp_reverse: there are no unique old atoms; logp_proposal = 0.0.")
            #define reverse attributes
            self.reverse_new_positions, self.reverse_rjmc_info, self.reverse_atoms_with_positions_reduced_potential, self.reverse_final_context_reduced_potential, self.reverse_neglected_angle_terms = None, None, None, None, None
            self.reverse_final_growth_system = None
            self.reverse_special_terms = None
            return 0.0

        # Compute log proposal probability for reverse direction
        _logger.info("logp_reverse: unique new atoms detected; proceeding to _logp_propose...")
        logp_proposal, new_positions, rjmc_info, atoms_with_positions_reduced_potential, final_context_reduced_potential, neglected_angle_terms, special_terms = self._logp_propose(top_proposal, old_coordinates, beta, new_positions=new_coordinates, direction='reverse')
        self.reverse_new_positions, self.reverse_rjmc_info = new_positions, rjmc_info
        self.reverse_atoms_with_positions_reduced_potential, self.reverse_final_context_reduced_potential = atoms_with_positions_reduced_potential, final_context_reduced_potential
        self.reverse_neglected_angle_terms = neglected_angle_terms
        self.reverse_special_terms = special_terms
        check_dimensionality(logp_proposal, float)
        return logp_proposal

    def _write_partial_pdb(self, pdbfile, topology, positions, atoms_with_positions, model_index):
        """
        Write the subset of the molecule for which positions are defined.

        Parameters
        ----------
        pdbfile : file-like object
            The open file-like object for the PDB file being written
        topology : simtk.openmm.Topology
            The OpenMM Topology object
        positions : simtk.unit.Quantity of shape (n_atoms, 3) with units compatible with nanometers
            The positions
        atoms_with_positions : list of parmed.Atom
            parmed Atom objects for which positions have been defined
        model_index : int
            The MODEL index for the PDB file to use

        """
        check_dimensionality(positions, unit.nanometers)

        from simtk.openmm.app import Modeller
        modeller = Modeller(topology, positions)
        atom_indices_with_positions = [ atom.idx for atom in atoms_with_positions ]
        atoms_to_delete = [ atom for atom in modeller.topology.atoms() if (atom.index not in atom_indices_with_positions) ]
        modeller.delete(atoms_to_delete)

        pdbfile.write('MODEL %5d\n' % model_index)
        from simtk.openmm.app import PDBFile
        PDBFile.writeFile(modeller.topology, modeller.positions, file=pdbfile)
        pdbfile.flush()
        pdbfile.write('ENDMDL\n')

    def _logp_propose(self, top_proposal, old_positions, beta, new_positions=None, direction='forward'):
        """
        This is an INTERNAL function that handles both the proposal and the logp calculation,
        to reduce code duplication. Whether it proposes or just calculates a logp is based on
        the direction option. Note that with respect to "new" and "old" terms, "new" will always
        mean the direction we are proposing (even in the reverse case), so that for a reverse proposal,
        this function will still take the new coordinates as new_coordinates

        Parameters
        ----------
        top_proposal : topology_proposal.TopologyProposal object
            topology proposal containing the relevant information
        old_positions : simtk.unit.Quantity with shape (n_atoms, 3) with units compatible with nanometers
            The coordinates of the system before the proposal
        beta : simtk.unit.Quantity with units compatible with 1/(kilojoules_per_mole)
            The inverse thermal energy
        new_positions : simtk.unit.Quantity with shape (n_atoms, 3) with units compatible with nanometers, optional, default=None
            The coordinates of the system after the proposal, or None for forward proposals
        direction : str
            Whether to make a proposal ('forward') or just calculate logp ('reverse')

        Returns
        -------
        logp_proposal : float
            the logp of the proposal
        new_positions : simtk.unit.Quantity with shape (n_atoms, 3) with units compatible with nanometers
            The new positions (same as input if direction='reverse')
        rjmc_info: list
            List of proposal information, of form [atom.idx, u_r, u_theta, r, theta, phi, logp_r, logp_theta, logp_phi, np.log(detJ), added_energy, proposal_prob]
        atoms_with_positions_reduced_potential : float
            energy of core atom configuration (i.e. before any proposal is made).
        final_context_reduced_potential : float
            enery of final system (corrected for valence-only and whether angles are neglected).  In reverse regime, this is the old system.
        neglected_angle_terms : list of ints
            list of indices corresponding to the angle terms in the corresponding system that are neglected (i.e. which are to be
            placed into the lambda perturbation scheme)
        growth_system_generator.special_terms : dict
            dict of special terms that are added to or omitted from the potential for stereochemical reasons (i.e. ring closures and stereocenters)
        """
        _logger.info("Conducting forward proposal...")
        import copy
        from perses.tests.utils import compute_potential_components
        # Ensure all parameters have the expected units
        check_dimensionality(old_positions, unit.angstroms)
        if new_positions is not None:
            check_dimensionality(new_positions, unit.angstroms)

        # Determine order in which atoms (and the torsions they are involved in) will be proposed
        _logger.info("Computing proposal order with NetworkX...")
        proposal_order_tool = NetworkXProposalOrder(top_proposal, direction=direction)
        torsion_proposal_order, logp_choice = proposal_order_tool.determine_proposal_order()
        atom_proposal_order = [ torsion[0] for torsion in torsion_proposal_order ]
        _logger.info(f"number of atoms to be placed: {len(atom_proposal_order)}")
        _logger.info(f"Atom index proposal order is {atom_proposal_order}")
        _logger.info(f"Torsion proposal order is: {torsion_proposal_order}")

        growth_parameter_name = 'growth_stage'
        if direction=="forward":
            _logger.info("direction of proposal is forward; creating atoms_with_positions and new positions from old system/topology...")
            # Find and copy known positions to match new topology
            import parmed
            structure = parmed.openmm.load_topology(top_proposal.new_topology, top_proposal.new_system)
            atoms_with_positions = [structure.atoms[atom_idx] for atom_idx in top_proposal.new_to_old_atom_map.keys()]
            new_positions = self._copy_positions(atoms_with_positions, top_proposal, old_positions)
            self._new_posits = copy.deepcopy(new_positions)

            # Create modified System object
            _logger.info("creating growth system...")
            growth_system_generator = GeometrySystemGenerator(top_proposal.new_system, torsion_proposal_order, global_parameter_name=growth_parameter_name, connectivity_graph = proposal_order_tool._connectivity_graph, reference_graph = proposal_order_tool._residue_graph, use_sterics=self.use_sterics, neglect_angles = self.neglect_angles, use_14_nonbondeds = self._use_14_nonbondeds)
            growth_system = growth_system_generator.get_modified_system()
            special_terms = growth_system_generator.special_terms

        elif direction=='reverse':
            _logger.info("direction of proposal is reverse; creating atoms_with_positions from old system/topology")
            if new_positions is None:
                raise ValueError("For reverse proposals, new_positions must not be none.")

            # Find and copy known positions to match old topology
            import parmed
            structure = parmed.openmm.load_topology(top_proposal.old_topology, top_proposal.old_system)
            atoms_with_positions = [structure.atoms[atom_idx] for atom_idx in top_proposal.old_to_new_atom_map.keys()]

            # Create modified System object
            _logger.info("creating growth system...")
            growth_system_generator = GeometrySystemGenerator(top_proposal.old_system, torsion_proposal_order, global_parameter_name=growth_parameter_name, connectivity_graph = proposal_order_tool._connectivity_graph, reference_graph = proposal_order_tool._residue_graph, use_sterics=self.use_sterics, neglect_angles = self.neglect_angles, use_14_nonbondeds = self._use_14_nonbondeds)
            growth_system = growth_system_generator.get_modified_system()
            special_terms = growth_system_generator.special_terms
        else:
            raise ValueError("Parameter 'direction' must be forward or reverse")

        # Define a system for the core atoms before new atoms are placed
        self.atoms_with_positions_system = growth_system_generator._atoms_with_positions_system
        self.growth_system = growth_system

        # Get the angle terms that are neglected from the growth system
        neglected_angle_terms = growth_system_generator.neglected_angle_terms
        _logger.info(f"neglected angle terms include {neglected_angle_terms}")

        # Rename the logp_choice from the NetworkXProposalOrder for the purpose of adding logPs in the growth stage
        logp_proposal = np.sum(np.array(logp_choice))
        _logger.info(f"log probability choice of torsions and atom order: {logp_proposal}")

        if self._storage:
            self._storage.write_object("{}_proposal_order".format(direction), proposal_order_tool, iteration=self.nproposed)

        if self.use_sterics:
            platform_name = 'CPU' # faster when sterics are in use
        else:
            platform_name = 'Reference' # faster when only valence terms are in use


        # Create an OpenMM context
        from simtk import openmm
        _logger.info("creating platform, integrators, and contexts; setting growth parameter")
        platform = openmm.Platform.getPlatformByName(platform_name)
        integrator = openmm.VerletIntegrator(1*unit.femtoseconds)
        atoms_with_positions_system_integrator = openmm.VerletIntegrator(1*unit.femtoseconds)
        final_system_integrator = openmm.VerletIntegrator(1*unit.femtoseconds)
        context = openmm.Context(growth_system, integrator, platform)
        growth_system_generator.set_growth_parameter_index(len(atom_proposal_order)+1, context)

        #create final growth contexts for nonalchemical perturbations...
        if direction == 'forward':
            self.forward_final_growth_system = copy.deepcopy(context.getSystem())
        elif direction == 'reverse':
            self.reverse_final_growth_system = copy.deepcopy(context.getSystem())

        growth_parameter_value = 1 # Initialize the growth_parameter value before the atom placement loop

        # In the forward direction, atoms_with_positions_system considers the atoms_with_positions
        # In the reverse direction, atoms_with_positions_system considers the old_positions of atoms in the
        atoms_with_positions_context = openmm.Context(self.atoms_with_positions_system, atoms_with_positions_system_integrator, platform)
        if direction == 'forward':
            _logger.info("setting atoms_with_positions context new positions")
            atoms_with_positions_context.setPositions(new_positions)
        else:
            _logger.info("setting atoms_with_positions context old positions")
            atoms_with_positions_context.setPositions(old_positions)

        #Print the energy of the system before unique_new/old atoms are placed...
        state = atoms_with_positions_context.getState(getEnergy=True)
        atoms_with_positions_reduced_potential = beta*state.getPotentialEnergy()
        atoms_with_positions_reduced_potential_components = [(force, energy*beta) for force, energy in compute_potential_components(atoms_with_positions_context)]
        atoms_with_positions_methods_differences = abs(atoms_with_positions_reduced_potential - sum([i[1] for i in atoms_with_positions_reduced_potential_components]))
        assert atoms_with_positions_methods_differences < ENERGY_THRESHOLD, f"the difference between the atoms_with_positions_reduced_potential and the sum of atoms_with_positions_reduced_potential_components is {abs(atoms_with_positions_reduced_potential - sum([i[1] for i in atoms_with_positions_reduced_potential_components]))}"

        # Place each atom in predetermined order
        _logger.info("There are {} new atoms".format(len(atom_proposal_order)))

        rjmc_info = list()
        energy_logger = [] #for bookkeeping per_atom energy reduced potentials

        for torsion_atom_indices, proposal_prob in zip(torsion_proposal_order, logp_choice):

            _logger.debug(f"Proposing torsion {torsion_atom_indices} with proposal probability {proposal_prob}")

            # Get parmed Structure Atom objects associated with torsion
            atom, bond_atom, angle_atom, torsion_atom = [ structure.atoms[index] for index in torsion_atom_indices ]

            # Activate the new atom interactions
            growth_system_generator.set_growth_parameter_index(growth_parameter_value, context=context)

            # Get internal coordinates if direction is reverse
            if direction == 'reverse':
                atom_coords, bond_coords, angle_coords, torsion_coords = [ old_positions[index] for index in torsion_atom_indices ]
                internal_coordinates, detJ = self._cartesian_to_internal(atom_coords, bond_coords, angle_coords, torsion_coords)
                # Extract dimensionless internal coordinates
                r, theta, phi = internal_coordinates[0], internal_coordinates[1], internal_coordinates[2] # dimensionless

                _logger.debug(f"\treverse proposal: r = {r}; theta = {theta}; phi = {phi}")

            bond = self._get_relevant_bond(atom, bond_atom)

            if bond is not None:
                if direction == 'forward':
                    r = self._propose_bond(bond, beta, self._n_bond_divisions)

                    _logger.debug(f"\tproposing forward bond of {r}.")

                logp_r = self._bond_logp(r, bond, beta, self._n_bond_divisions)
                _logger.debug(f"\tlogp_r = {logp_r}.")

                # Retrieve relevant quantities for valence bond and compute u_r
                r0, k = bond.type.req, bond.type.k * self._bond_softening_constant
                sigma_r = unit.sqrt((1.0/(beta*k)))
                r0, k, sigma_r = r0.value_in_unit_system(unit.md_unit_system), k.value_in_unit_system(unit.md_unit_system), sigma_r.value_in_unit_system(unit.md_unit_system)
                u_r = 0.5*((r - r0)/sigma_r)**2

                _logger.debug(f"\treduced r potential = {u_r}.")

            else:
                if direction == 'forward':
                    constraint = self._get_bond_constraint(atom, bond_atom, top_proposal.new_system)
                    if constraint is None:
                        raise ValueError("Structure contains a topological bond [%s - %s] with no constraint or bond information." % (str(atom), str(bond_atom)))

                    r = constraint.value_in_unit_system(unit.md_unit_system) #set bond length to exactly constraint
                    _logger.debug(f"\tproposing forward constrained bond of {r} with log probability of 0.0 and implied u_r of 0.0.")

                logp_r = 0.0
                u_r = 0.0

            # Propose an angle and calculate its log probability
            angle = self._get_relevant_angle(atom, bond_atom, angle_atom)
            if direction=='forward':
                theta = self._propose_angle(angle, beta, self._n_angle_divisions)
                _logger.debug(f"\tproposing forward angle of {theta}.")

            logp_theta = self._angle_logp(theta, angle, beta, self._n_angle_divisions)
            _logger.debug(f"\t logp_theta = {logp_theta}.")

            # Retrieve relevant quantities for valence angle and compute u_theta
            theta0, k = angle.type.theteq, angle.type.k * self._angle_softening_constant
            sigma_theta = unit.sqrt(1.0/(beta * k))
            theta0, k, sigma_theta = theta0.value_in_unit_system(unit.md_unit_system), k.value_in_unit_system(unit.md_unit_system), sigma_theta.value_in_unit_system(unit.md_unit_system)
            u_theta = 0.5*((theta - theta0)/sigma_theta)**2
            _logger.info(f"\treduced angle potential = {u_theta}.")

            # Propose a torsion angle and calcualate its log probability
            if direction=='forward':
                # Note that (r, theta) are dimensionless here
                phi, logp_phi = self._propose_torsion(context, torsion_atom_indices, new_positions, r, theta, beta, self._n_torsion_divisions)
                xyz, detJ = self._internal_to_cartesian(new_positions[bond_atom.idx], new_positions[angle_atom.idx], new_positions[torsion_atom.idx], r, theta, phi)
                new_positions[atom.idx] = xyz

                _logger.debug(f"\tproposing forward torsion of {phi}.")
                _logger.debug(f"\tsetting new_positions[{atom.idx}] to {xyz}. ")
            else:
                old_positions_for_torsion = copy.deepcopy(old_positions)
                # Note that (r, theta, phi) are dimensionless here
                logp_phi = self._torsion_logp(context, torsion_atom_indices, old_positions_for_torsion, r, theta, phi, beta, self._n_torsion_divisions)
            _logger.debug(f"\tlogp_phi = {logp_phi}")


            # Compute potential energy
            if direction == 'forward':
                context.setPositions(new_positions)
            else:
                context.setPositions(old_positions)

            state = context.getState(getEnergy=True)
            reduced_potential_energy = beta*state.getPotentialEnergy()
            _logger.debug(f"\taccumulated growth context reduced energy = {reduced_potential_energy}")


            #Compute change in energy from previous reduced potential
            if growth_parameter_value == 1: # then there is no previous reduced potential so u_phi is simply reduced_potential_energy - u_r - u_theta
                added_energy = reduced_potential_energy
            else:
                previous_reduced_potential_energy = energy_logger[-1]
                added_energy = reduced_potential_energy - previous_reduced_potential_energy

            _logger.debug(f"growth index {growth_parameter_value} added reduced energy = {added_energy}.")

            atom_placement_dict = {'atom_index': atom.idx,
                                   'u_r': u_r,
                                   'u_theta' : u_theta,
                                   'r': r,
                                   'theta': theta,
                                   'phi': phi,
                                   'logp_r': logp_r,
                                   'logp_theta': logp_theta,
                                   'logp_phi': logp_phi,
                                   'log_detJ': np.log(detJ),
                                   'added_energy': added_energy,
                                   'proposal_prob': proposal_prob}
            rjmc_info.append(atom_placement_dict)

            logp_proposal += logp_r + logp_theta + logp_phi - np.log(detJ) # TODO: Check sign of detJ
            growth_parameter_value += 1
            energy_logger.append(reduced_potential_energy)
            # DEBUG: Write PDB file for placed atoms
            atoms_with_positions.append(atom)
            _logger.debug(f"\tatom placed, rjmc_info list updated, and growth_parameter_value incremented.")


        # assert that the energy of the new positions is ~= atoms_with_positions_reduced_potential + reduced_potential_energy
        # The final context is treated in the same way as the atoms_with_positions_context
        if direction == 'forward': #if the direction is forward, the final system for comparison is top_proposal's new system
            _system, _positions = top_proposal._new_system, new_positions
        else:
            _system, _positions = top_proposal._old_system, old_positions

        if not self.use_sterics:
            final_system = self._define_no_nb_system(_system, neglected_angle_terms, atom_proposal_order, growth_system_generator.special_terms)
            _logger.info(f"{direction} final system defined with {len(neglected_angle_terms)} neglected angles.")
        else:
            final_system = copy.deepcopy(_system)
            force_names = {force.__class__.__name__ : index for index, force in enumerate(final_system.getForces())}
            if 'NonbondedForce' in force_names.keys():
                final_system.getForce(force_names['NonbondedForce']).setUseDispersionCorrection(False)
            _logger.info(f"{direction} final system defined with nonbonded interactions.")
        final_context = openmm.Context(final_system, final_system_integrator, platform)
        final_context.setPositions(_positions)

        state = final_context.getState(getEnergy=True)
        final_context_reduced_potential = beta*state.getPotentialEnergy()
        final_context_components = [(force, energy*beta) for force, energy in compute_potential_components(final_context)]
        atoms_with_positions_reduced_potential_components = [(force, energy*beta) for force, energy in compute_potential_components(atoms_with_positions_context)]
        _logger.debug(f"reduced potential components before atom placement:")
        for item in atoms_with_positions_reduced_potential_components:
            _logger.debug(f"\t\t{item[0]}: {item[1]}")
        _logger.info(f"total reduced potential before atom placement: {atoms_with_positions_reduced_potential}")

        _logger.info(f"potential components added from growth system:")
        added_energy_components = [(force, energy*beta) for force, energy in compute_potential_components(context)]
        for item in added_energy_components:
            _logger.info(f"\t\t{item[0]}: {item[1]}")
        _logger.info(f"total reduced energy added from growth system: {reduced_potential_energy}")

        _logger.info(f"reduced potential of final system:")
        for item in final_context_components:
            _logger.info(f"\t\t{item[0]}: {item[1]}")
        _logger.info(f"final reduced energy {final_context_reduced_potential}")

        _logger.info(f"sum of energies: {atoms_with_positions_reduced_potential + reduced_potential_energy}")
        _logger.info(f"magnitude of difference in the energies: {abs(final_context_reduced_potential - atoms_with_positions_reduced_potential - reduced_potential_energy)}")

        energy_mismatch_ratio = (atoms_with_positions_reduced_potential + reduced_potential_energy) / (final_context_reduced_potential)
        assert (energy_mismatch_ratio < ENERGY_MISMATCH_RATIO_THRESHOLD + 1) and (energy_mismatch_ratio > 1 - ENERGY_MISMATCH_RATIO_THRESHOLD)  , f"The ratio of the calculated final energy to the true final energy is {energy_mismatch_ratio}"

        # Final log proposal:
        _logger.info("Final logp_proposal: {}".format(logp_proposal))
        # Clean up OpenMM Context since garbage collector is sometimes slow
        del context; del atoms_with_positions_context; del final_context
        del integrator; del atoms_with_positions_system_integrator; del final_system_integrator

        check_dimensionality(logp_proposal, float)
        check_dimensionality(new_positions, unit.nanometers)

        if self.use_sterics:
            return logp_proposal, new_positions, rjmc_info, 0.0, reduced_potential_energy, []

        return logp_proposal, new_positions, rjmc_info, atoms_with_positions_reduced_potential, final_context_reduced_potential, neglected_angle_terms, growth_system_generator.special_terms

    @staticmethod
    def _oemol_from_residue(res, verbose=True):
        """
        Get an OEMol from a residue, even if that residue
        is polymeric. In the latter case, external bonds
        are replaced by hydrogens.

        Parameters
        ----------
        res : app.Residue
            The residue in question
        verbose : bool, optional, default=False
            If True, will print verbose output.

        Returns
        -------
        oemol : openeye.oechem.OEMol
            an oemol representation of the residue with topology indices
        """
        # TODO: Deprecate this
        from openeye import oechem
        from simtk.openmm import app

        # TODO: This seems to be broken. Can we fix it?
        from openmoltools.forcefield_generators import generateOEMolFromTopologyResidue
        external_bonds = list(res.external_bonds())
        for bond in external_bonds:
            if verbose: print(bond)
        new_atoms = {}
        highest_index = 0
        if external_bonds:
            new_topology = app.Topology()
            new_chain = new_topology.addChain(0)
            new_res = new_topology.addResidue("new_res", new_chain)
            for atom in res.atoms():
                new_atom = new_topology.addAtom(atom.name, atom.element, new_res, atom.id)
                new_atom.index = atom.index
                new_atoms[atom] = new_atom
                highest_index = max(highest_index, atom.index)
            for bond in res.internal_bonds():
                new_topology.addBond(new_atoms[bond[0]], new_atoms[bond[1]])
            for bond in res.external_bonds():
                internal_atom = [atom for atom in bond if atom.residue==res][0]
                if verbose:
                    print('internal atom')
                    print(internal_atom)
                highest_index += 1
                if internal_atom.name=='N':
                    if verbose: print('Adding H to N')
                    new_atom = new_topology.addAtom("H2", app.Element.getByAtomicNumber(1), new_res, -1)
                    new_atom.index = -1
                    new_topology.addBond(new_atoms[internal_atom], new_atom)
                if internal_atom.name=='C':
                    if verbose: print('Adding OH to C')
                    new_atom = new_topology.addAtom("O2", app.Element.getByAtomicNumber(8), new_res, -1)
                    new_atom.index = -1
                    new_topology.addBond(new_atoms[internal_atom], new_atom)
                    highest_index += 1
                    new_hydrogen = new_topology.addAtom("HO", app.Element.getByAtomicNumber(1), new_res, -1)
                    new_hydrogen.index = -1
                    new_topology.addBond(new_hydrogen, new_atom)
            res_to_use = new_res
            external_bonds = list(res_to_use.external_bonds())
        else:
            res_to_use = res
        oemol = generateOEMolFromTopologyResidue(res_to_use, geometry=False)
        oechem.OEAddExplicitHydrogens(oemol)
        return oemol

    def _define_no_nb_system(self, system, neglected_angle_terms, atom_proposal_order, special_terms):
        """
        This is a quick internal function to generate a final system for an assertion comparison with the energy added in the geometry proposal to the final
        energy.  Specifically, this function generates a final system (neglecting nonbonded interactions and specified valence terms)

        Parameters
        ----------
        system : openmm.app.System object
            system of the target (from the topology proposal), which should include all valence, steric, and electrostatic terms
        neglected_angle_terms : list of ints
            list of HarmonicAngleForce indices corresponding to the neglected terms
        atom_proposal_order : int list
            defines the order in which atoms are proposed
        special_terms : GeometrySystemGenerator.special_terms dict
            defines the terms in the growth system that are omitted as a consequence of ring closure


        Returns
        -------
        final_system : openmm.app.System object
            final system for energy comparison

        """
        import copy
        from simtk import openmm, unit
        import networkx as nx
        no_nb_system = copy.deepcopy(system)
        _logger.info("\tbeginning construction of no_nonbonded final system...")

        num_forces = no_nb_system.getNumForces()
        for index in reversed(range(num_forces)):
            force = no_nb_system.getForce(index)
            _logger.debug(f"\t\tforce: {force}")
            if force.__class__.__name__ == 'HarmonicBondForce':
                num_bonds = force.getNumBonds()
                for bond_idx in range(num_bonds):
                    p1, p2, r0, k = force.getBondParameters(bond_idx)
                    if ((p1, p2) in special_terms['omitted_bonds']) or ((p1, p2)[::-1] in special_terms['omitted_bonds']):
                        force.setBondParameters(bond_idx, p1, p2, r0, k*0.0)
            if force.__class__.__name__ == 'HarmonicAngleForce':
                num_angles = force.getNumAngles()
                for angle_idx in range(num_angles):
                    p1, p2, p3, theta0, k = force.getAngleParameters(angle_idx)
                    if (p1, p2, p3) in special_terms['omitted_angles'] or (p1, p2, p3)[::-1] in special_terms['omitted_angles']:
                        force.setAngleParameters(angle_idx, p1, p2, p3, theta0, k*0.0)
                for angle in special_terms['extra_angles']:
                    force.addAngle(*angle)
            if force.__class__.__name__ == 'PeriodicTorsionForce':
                for torsion in special_terms['extra_torsions']:
                    _logger.debug(f"\t\t\ttorsion terms: {torsion}")
                    force.addTorsion(torsion[0], torsion[1], torsion[2], torsion[3], torsion[4][0], torsion[4][1], torsion[4][2])
                num_torsions = force.getNumTorsions()
                for torsion_idx in range(num_torsions):
                    p1, p2, p3, p4, periodicity, phi0, k = force.getTorsionParameters(torsion_idx)
                    if (p1, p2, p3, p4) in special_terms['omitted_torsions'] or (p1, p2, p3, p4)[::-1] in special_terms['omitted_torsions']:
                        force.setTorsionParameters(torsion_idx, p1, p2, p3, p4, periodicity, phi0, k*0.0)
            elif force.__class__.__name__ == 'NonbondedForce' or force.__class__.__name__ == 'MonteCarloBarostat':
                if self._use_14_nonbondeds and force.__class__.__name__ == 'NonbondedForce':
                    for particle_index in range(force.getNumParticles()):
                        [charge, sigma, epsilon] = force.getParticleParameters(particle_index)
                        force.setParticleParameters(particle_index, charge*0.0, sigma, epsilon*0.0)

                    for exception_index in range(force.getNumExceptions()):
                        p1, p2, chargeprod, sigma, epsilon = force.getExceptionParameters(exception_index)
                        if len(set(atom_proposal_order).intersection(set([p1, p2]))) == 0: #there is no growth index in this exception
                            #both particles are in core, so we omit the term in the excetions
                            force.setExceptionParameters(exception_index, p1, p2, chargeProd = chargeprod * 0.0, sigma = sigma, epsilon = epsilon * 0.0)
                        elif (p1, p2) in special_terms['omitted_1,4s']:
                            #we have to omit the pair 1,4 interaction since it is omitted in the growth system
                            force.setExceptionParameters(exception_index, p1, p2, chargeProd = chargeprod * 0.0, sigma = sigma, epsilon = epsilon * 0.0)
                        else: #both particles are observed terms in the growth system, so we include them
                            pass

                else:
                    no_nb_system.removeForce(index)

            elif force.__class__.__name__ == 'HarmonicAngleForce':
                num_angles = force.getNumAngles()
                for angle_idx in neglected_angle_terms:
                    p1, p2, p3, theta0, K = force.getAngleParameters(angle_idx)
                    force.setAngleParameters(angle_idx, p1, p2, p3, theta0, unit.Quantity(value=0.0, unit=unit.kilojoule/(unit.mole*unit.radian**2)))

        forces = no_nb_system.getForces()
        _logger.debug(f"\tfinal no-nonbonded final system forces {[force.__class__.__name__ for force in list(no_nb_system.getForces())]}")
        #bonds
        bond_forces = no_nb_system.getForce(0)
        _logger.debug(f"\tthere are {bond_forces.getNumBonds()} bond forces in the no-nonbonded final system")

        #angles
        angle_forces = no_nb_system.getForce(1)
        _logger.debug(f"\tthere are {angle_forces.getNumAngles()} angle forces in the no-nonbonded final system")

        #torsions
        torsion_forces = no_nb_system.getForce(2)
        _logger.debug(f"\tthere are {torsion_forces.getNumTorsions()} torsion forces in the no-nonbonded final system")


        return no_nb_system

    def _copy_positions(self, atoms_with_positions, top_proposal, current_positions):
        """
        Copy the current positions to an array that will also hold new positions
        Parameters
        ----------
        atoms_with_positions : list of parmed.Atom
            parmed Atom objects denoting atoms that currently have positions
        top_proposal : topology_proposal.TopologyProposal
            topology proposal object
        current_positions : simtk.unit.Quantity with shape (n_atoms, 3) with units compatible with nanometers
            Positions of the current system

        Returns
        -------
        new_positions : simtk.unit.Quantity with shape (n_atoms, 3) with units compatible with nanometers
            New positions for new topology object with known positions filled in
        """
        check_dimensionality(current_positions, unit.nanometers)

        # Create new positions
        new_shape = [top_proposal.n_atoms_new, 3]
        # Workaround for CustomAngleForce NaNs: Create random non-zero positions for new atoms.
        new_positions = unit.Quantity(np.random.random(new_shape), unit=unit.nanometers)

        # Copy positions for atoms that have them defined
        for atom in atoms_with_positions:
            old_index = top_proposal.new_to_old_atom_map[atom.idx]
            new_positions[atom.idx] = current_positions[old_index]

        check_dimensionality(new_positions, unit.nanometers)
        return new_positions

    def _get_relevant_bond(self, atom1, atom2):
        """
        Get parmaeters defining the bond connecting two atoms

        Parameters
        ----------
        atom1 : parmed.Atom
             One of the atoms in the bond
        atom2 : parmed.Atom object
             The other atom in the bond

        Returns
        -------
        bond : parmed.Bond with units modified to simtk.unit.Quantity
            Bond connecting the two atoms, or None if constrained or no bond term exists.
            Parameters representing unit-bearing quantities have been converted to simtk.unit.Quantity with units attached.
        """
        bonds_1 = set(atom1.bonds)
        bonds_2 = set(atom2.bonds)
        relevant_bond_set = bonds_1.intersection(bonds_2)
        relevant_bond = relevant_bond_set.pop()
        if relevant_bond.type is None:
            return None
        relevant_bond_with_units = self._add_bond_units(relevant_bond)

        check_dimensionality(relevant_bond_with_units.type.req, unit.nanometers)
        check_dimensionality(relevant_bond_with_units.type.k, unit.kilojoules_per_mole/unit.nanometers**2)
        return relevant_bond_with_units

    def _get_bond_constraint(self, atom1, atom2, system):
        """
        Get constraint parameters corresponding to the bond between the given atoms

        Parameters
        ----------
        atom1 : parmed.Atom
           The first atom of the constrained bond
        atom2 : parmed.Atom
           The second atom of the constrained bond
        system : openmm.System
           The system containing the constraint

        Returns
        -------
        constraint : simtk.unit.Quantity or None
            If a constraint is defined between the two atoms, the length is returned; otherwise None
        """
        # TODO: This algorithm is incredibly inefficient.
        # Instead, generate a dictionary lookup of constrained distances.

        atom_indices = set([atom1.idx, atom2.idx])
        n_constraints = system.getNumConstraints()
        constraint = None
        for i in range(n_constraints):
            p1, p2, length = system.getConstraintParameters(i)
            constraint_atoms = set([p1, p2])
            if len(constraint_atoms.intersection(atom_indices))==2:
                constraint = length

        if constraint is not None:
            check_dimensionality(constraint, unit.nanometers)
        return constraint

    def _get_relevant_angle(self, atom1, atom2, atom3):
        """
        Get the angle containing the 3 given atoms

        Parameters
        ----------
        atom1 : parmed.Atom
            The first atom defining the angle
        atom2 : parmed.Atom
            The second atom defining the angle
        atom3 : parmed.Atom
            The third atom in the angle

        Returns
        -------
        relevant_angle_with_units : parmed.Angle with parmeters modified to be simtk.unit.Quantity
            Angle connecting the three atoms
            Parameters representing unit-bearing quantities have been converted to simtk.unit.Quantity with units attached.
        """
        atom1_angles = set(atom1.angles)
        atom2_angles = set(atom2.angles)
        atom3_angles = set(atom3.angles)
        relevant_angle_set = atom1_angles.intersection(atom2_angles, atom3_angles)

        # DEBUG
        if len(relevant_angle_set) == 0:
            print('atom1_angles:')
            print(atom1_angles)
            print('atom2_angles:')
            print(atom2_angles)
            print('atom3_angles:')
            print(atom3_angles)
            raise Exception('Atoms %s-%s-%s do not share a parmed Angle term' % (atom1, atom2, atom3))

        relevant_angle = relevant_angle_set.pop()
        if type(relevant_angle.type.k) != unit.Quantity:
            relevant_angle_with_units = self._add_angle_units(relevant_angle)
        else:
            relevant_angle_with_units = relevant_angle

        check_dimensionality(relevant_angle.type.theteq, unit.radians)
        check_dimensionality(relevant_angle.type.k, unit.kilojoules_per_mole/unit.radians**2)
        return relevant_angle_with_units

    def _add_bond_units(self, bond):
        """
        Attach units to a parmed harmonic bond

        Parameters
        ----------
        bond : parmed.Bond
            The bond object whose paramters will be converted to unit-bearing quantities

        Returns
        -------
        bond : parmed.Bond with units modified to simtk.unit.Quantity
            The same modified Bond object that was passed in
            Parameters representing unit-bearing quantities have been converted to simtk.unit.Quantity with units attached.

        """
        # TODO: Shouldn't we be making a deep copy?

        # If already promoted to unit-bearing quantities, return the object
        if type(bond.type.k)==unit.Quantity:
            return bond
        # Add parmed units
        # TODO: Get rid of this, and just operate on the OpenMM System instead
        bond.type.req = unit.Quantity(bond.type.req, unit=unit.angstrom)
        bond.type.k = unit.Quantity(2.0*bond.type.k, unit=unit.kilocalorie_per_mole/unit.angstrom**2)
        return bond

    def _add_angle_units(self, angle):
        """
        Attach units to parmed harmonic angle

        Parameters
        ----------
        angle : parmed.Angle
            The angle object whose paramters will be converted to unit-bearing quantities

        Returns
        -------
        angle : parmed.Angle with units modified to simtk.unit.Quantity
            The same modified Angle object that was passed in
            Parameters representing unit-bearing quantities have been converted to simtk.unit.Quantity with units attached.

        """
        # TODO: Shouldn't we be making a deep copy?

        # If already promoted to unit-bearing quantities, return the object
        if type(angle.type.k)==unit.Quantity:
            return angle
        # Add parmed units
        # TODO: Get rid of this, and just operate on the OpenMM System instead
        angle.type.theteq = unit.Quantity(angle.type.theteq, unit=unit.degree)
        angle.type.k = unit.Quantity(2.0*angle.type.k, unit=unit.kilocalorie_per_mole/unit.radian**2)
        return angle

    def _add_torsion_units(self, torsion):
        """
        Add the correct units to a torsion

        Parameters
        ----------
        torsion : parmed.Torsion
            The angle object whose paramters will be converted to unit-bearing quantities

        Returns
        -------
        torsion : parmed.Torsion with units modified to simtk.unit.Quantity
            The same modified Torsion object that was passed in
            Parameters representing unit-bearing quantities have been converted to simtk.unit.Quantity with units attached.

        """
        # TODO: Shouldn't we be making a deep copy?

        # If already promoted to unit-bearing quantities, return the object
        if type(torsion.type.phi_k) == unit.Quantity:
            return torsion
        # Add parmed units
        # TODO: Get rid of this, and just operate on the OpenMM System instead
        torsion.type.phi_k = unit.Quantity(torsion.type.phi_k, unit=unit.kilocalorie_per_mole)
        torsion.type.phase = unit.Quantity(torsion.type.phase, unit=unit.degree)
        return torsion

    def _rotation_matrix(self, axis, angle):
        """
        Compute a rotation matrix about the origin given a coordinate axis and an angle.

        Parameters
        ----------
        axis : ndarray of shape (3,) without units
            The axis about which rotation should occur
        angle : float (implicitly in radians)
            The angle of rotation about the axis

        Returns
        -------
        rotation_matrix : ndarray of shape (3,3) without units
            The 3x3 rotation matrix
        """
        axis = axis/np.linalg.norm(axis)
        axis_squared = np.square(axis)
        cos_angle = np.cos(angle)
        sin_angle = np.sin(angle)
        rot_matrix_row_one = np.array([cos_angle+axis_squared[0]*(1-cos_angle),
                                       axis[0]*axis[1]*(1-cos_angle) - axis[2]*sin_angle,
                                       axis[0]*axis[2]*(1-cos_angle)+axis[1]*sin_angle])

        rot_matrix_row_two = np.array([axis[1]*axis[0]*(1-cos_angle)+axis[2]*sin_angle,
                                      cos_angle+axis_squared[1]*(1-cos_angle),
                                      axis[1]*axis[2]*(1-cos_angle) - axis[0]*sin_angle])

        rot_matrix_row_three = np.array([axis[2]*axis[0]*(1-cos_angle)-axis[1]*sin_angle,
                                        axis[2]*axis[1]*(1-cos_angle)+axis[0]*sin_angle,
                                        cos_angle+axis_squared[2]*(1-cos_angle)])

        rotation_matrix = np.array([rot_matrix_row_one, rot_matrix_row_two, rot_matrix_row_three])
        return rotation_matrix

    def _cartesian_to_internal(self, atom_position, bond_position, angle_position, torsion_position):
        """
        Cartesian to internal coordinate conversion

        Parameters
        ----------
        atom_position : simtk.unit.Quantity wrapped numpy array of shape (natoms,) with units compatible with nanometers
            Position of atom whose internal coordinates are to be computed with respect to other atoms
        bond_position : simtk.unit.Quantity wrapped numpy array of shape (natoms,) with units compatible with nanometers
            Position of atom separated from newly placed atom with bond length ``r``
        angle_position : simtk.unit.Quantity wrapped numpy array of shape (natoms,) with units compatible with nanometers
            Position of atom separated from newly placed atom with angle ``theta``
        torsion_position : simtk.unit.Quantity wrapped numpy array of shape (natoms,) with units compatible with nanometers
            Position of atom separated from newly placed atom with torsion ``phi``

        Returns
        -------
        internal_coords : tuple of (float, float, float)
            Tuple representing (r, theta, phi):
            r : float (implicitly in nanometers)
                Bond length distance from ``bond_position`` to newly placed atom
            theta : float (implicitly in radians on domain [0,pi])
                Angle formed by ``(angle_position, bond_position, new_atom)``
            phi : float (implicitly in radians on domain [-pi, +pi))
                Torsion formed by ``(torsion_position, angle_position, bond_position, new_atom)``
        detJ : float
            The absolute value of the determinant of the Jacobian transforming from (r,theta,phi) to (x,y,z)
            .. todo :: Clarify the direction of the Jacobian

        """
        # TODO: _cartesian_to_internal and _internal_to_cartesian should accept/return units and have matched APIs

        check_dimensionality(atom_position, unit.nanometers)
        check_dimensionality(bond_position, unit.nanometers)
        check_dimensionality(angle_position, unit.nanometers)
        check_dimensionality(torsion_position, unit.nanometers)

        # Convert to internal coordinates once everything is dimensionless
        # Make sure positions are float64 arrays implicitly in units of nanometers for numba
        from perses.rjmc import coordinate_numba
        internal_coords = coordinate_numba.cartesian_to_internal(
            atom_position.value_in_unit(unit.nanometers).astype(np.float64),
            bond_position.value_in_unit(unit.nanometers).astype(np.float64),
            angle_position.value_in_unit(unit.nanometers).astype(np.float64),
            torsion_position.value_in_unit(unit.nanometers).astype(np.float64))
        # Return values are also in floating point implicitly in nanometers and radians
        r, theta, phi = internal_coords

        # Compute absolute value of determinant of Jacobian
        detJ = np.abs(r**2*np.sin(theta))

        check_dimensionality(r, float)
        check_dimensionality(theta, float)
        check_dimensionality(phi, float)
        check_dimensionality(detJ, float)

        return internal_coords, detJ

    def _internal_to_cartesian(self, bond_position, angle_position, torsion_position, r, theta, phi):
        """
        Calculate the cartesian coordinates of a newly placed atom in terms of internal coordinates,
        along with the absolute value of the determinant of the Jacobian.

        Parameters
        ----------
        bond_position : simtk.unit.Quantity wrapped numpy array of shape (natoms,) with units compatible with nanometers
            Position of atom separated from newly placed atom with bond length ``r``
        angle_position : simtk.unit.Quantity wrapped numpy array of shape (natoms,) with units compatible with nanometers
            Position of atom separated from newly placed atom with angle ``theta``
        torsion_position : simtk.unit.Quantity wrapped numpy array of shape (natoms,) with units compatible with nanometers
            Position of atom separated from newly placed atom with torsion ``phi``
        r : simtk.unit.Quantity with units compatible with nanometers
            Bond length distance from ``bond_position`` to newly placed atom
        theta : simtk.unit.Quantity with units compatible with radians
            Angle formed by ``(angle_position, bond_position, new_atom)``
        phi : simtk.unit.Quantity with units compatible with radians
            Torsion formed by ``(torsion_position, angle_position, bond_position, new_atom)``

        Returns
        -------
        xyz : simtk.unit.Quantity wrapped numpy array of shape (3,) with units compatible with nanometers
            The position of the newly placed atom
        detJ : float
            The absolute value of the determinant of the Jacobian transforming from (r,theta,phi) to (x,y,z)
            .. todo :: Clarify the direction of the Jacobian

        """
        # TODO: _cartesian_to_internal and _internal_to_cartesian should accept/return units and have matched APIs

        check_dimensionality(bond_position, unit.nanometers)
        check_dimensionality(angle_position, unit.nanometers)
        check_dimensionality(torsion_position, unit.nanometers)
        check_dimensionality(r, float)
        check_dimensionality(theta, float)
        check_dimensionality(phi, float)

        # Compute Cartesian coordinates from internal coordinates using all-dimensionless quantities
        # All inputs to numba must be in float64 arrays implicitly in md_unit_syste units of nanometers and radians
        from perses.rjmc import coordinate_numba
        xyz = coordinate_numba.internal_to_cartesian(
            bond_position.value_in_unit(unit.nanometers).astype(np.float64),
            angle_position.value_in_unit(unit.nanometers).astype(np.float64),
            torsion_position.value_in_unit(unit.nanometers).astype(np.float64),
            np.array([r, theta, phi], np.float64))
        # Transform position of new atom back into unit-bearing Quantity
        xyz = unit.Quantity(xyz, unit=unit.nanometers)

        # Compute abs det Jacobian using unitless values
        detJ = np.abs(r**2*np.sin(theta))

        check_dimensionality(xyz, unit.nanometers)
        check_dimensionality(detJ, float)
        return xyz, detJ

    def _bond_log_pmf(self, bond, beta, n_divisions):
        """
        Calculate the log probability mass function (PMF) of drawing a bond.

        .. math ::

            p(r; \beta, K_r, r_0) \propto r^2 e^{-\frac{\beta K_r}{2} (r - r_0)^2 }

        Prameters
        ---------
        bond : parmed.Structure.Bond modified to use simtk.unit.Quantity
            Valence bond parameters
        beta : simtk.unit.Quantity with units compatible with 1/kilojoules_per_mole
            Inverse thermal energy
        n_divisions : int
            Number of quandrature points for drawing bond length

        Returns
        -------
        r_i : np.ndarray of shape (n_divisions,) implicitly in units of nanometers
            r_i[i] is the bond length leftmost bin edge with corresponding log probability mass function p_i[i]
        log_p_i : np.ndarray of shape (n_divisions,)
            log_p_i[i] is the corresponding log probability mass of bond length r_i[i]
        bin_width : float implicitly in units of nanometers
            The bin width for individual PMF bins


        .. todo :: In future, this approach will be improved by eliminating discrete quadrature.

        """
        # TODO: Overhaul this method to accept and return unit-bearing quantities
        # TODO: We end up computing the discretized PMF over and over again; we can speed this up by caching
        # TODO: Switch from simple discrete quadrature to more sophisticated computation of pdf

        # Check input argument dimensions
        assert check_dimensionality(bond.type.req, unit.angstroms)
        assert check_dimensionality(bond.type.k, unit.kilojoules_per_mole/unit.nanometers**2)
        assert check_dimensionality(beta, unit.kilojoules_per_mole**(-1))

        # Retrieve relevant quantities for valence bond
        r0 = bond.type.req # equilibrium bond distance, unit-bearing quantity
        k = bond.type.k * self._bond_softening_constant # force constant, unit-bearing quantity
        sigma_r = unit.sqrt((1.0/(beta*k))) # standard deviation, unit-bearing quantity

        # Convert to dimensionless quantities in MD unit system
        r0 = r0.value_in_unit_system(unit.md_unit_system)
        k = k.value_in_unit_system(unit.md_unit_system)
        sigma_r = sigma_r.value_in_unit_system(unit.md_unit_system)

        # Determine integration bounds
        lower_bound, upper_bound = max(0., r0 - 6*sigma_r), (r0 + 6*sigma_r)

        # Compute integration quadrature points
        r_i, bin_width = np.linspace(lower_bound, upper_bound, num=n_divisions, retstep=True, endpoint=False)

        # Form log probability
        from scipy.special import logsumexp
        log_p_i = 2*np.log(r_i+(bin_width/2.0)) - 0.5*((r_i+(bin_width/2.0)-r0)/sigma_r)**2
        log_p_i -= logsumexp(log_p_i)

        check_dimensionality(r_i, float)
        check_dimensionality(log_p_i, float)
        check_dimensionality(bin_width, float)

        return r_i, log_p_i, bin_width

    def _bond_logp(self, r, bond, beta, n_divisions):
        """
        Calculate the log-probability of a given bond at a given inverse temperature

        Propose dimensionless bond length r from distribution

        .. math ::

            r \sim p(r; \beta, K_r, r_0) \propto r^2 e^{-\frac{\beta K_r}{2} (r - r_0)^2 }

        Prameters
        ---------
        r : float
            bond length, implicitly in nanometers
        bond : parmed.Structure.Bond modified to use simtk.unit.Quantity
            Valence bond parameters
        beta : simtk.unit.Quantity with units compatible with 1/kilojoules_per_mole
            Inverse thermal energy
        n_divisions : int
            Number of quandrature points for drawing bond length

        .. todo :: In future, this approach will be improved by eliminating discrete quadrature.

        """
        # TODO: Overhaul this method to accept and return unit-bearing quantities
        # TODO: Switch from simple discrete quadrature to more sophisticated computation of pdf

        check_dimensionality(r, float)
        check_dimensionality(beta, 1/unit.kilojoules_per_mole)

        r_i, log_p_i, bin_width = self._bond_log_pmf(bond, beta, n_divisions)

        if (r < r_i[0]) or (r >= r_i[-1] + bin_width):
            return LOG_ZERO

        # Determine index that r falls within
        index = int((r - r_i[0])/bin_width)
        assert (index >= 0) and (index < n_divisions)

        # Correct for division size
        logp = log_p_i[index] - np.log(bin_width)

        return logp

    def _propose_bond(self, bond, beta, n_divisions):
        """
        Propose dimensionless bond length r from distribution

        .. math ::

            r \sim p(r; \beta, K_r, r_0) \propto r^2 e^{-\frac{\beta K_r}{2} (r - r_0)^2 }

        Prameters
        ---------
        bond : parmed.Structure.Bond modified to use simtk.unit.Quantity
            Valence bond parameters
        beta : simtk.unit.Quantity with units compatible with 1/kilojoules_per_mole
            Inverse thermal energy
        n_divisions : int
            Number of quandrature points for drawing bond length

        Returns
        -------
        r : float
            Dimensionless bond length, implicitly in nanometers

        .. todo :: In future, this approach will be improved by eliminating discrete quadrature.

        """
        # TODO: Overhaul this method to accept and return unit-bearing quantities
        # TODO: Switch from simple discrete quadrature to more sophisticated computation of pdf

        check_dimensionality(beta, 1/unit.kilojoules_per_mole)

        r_i, log_p_i, bin_width = self._bond_log_pmf(bond, beta, n_divisions)

        # Draw an index
        index = np.random.choice(range(n_divisions), p=np.exp(log_p_i))
        r = r_i[index]

        # Draw uniformly in that bin
        r = np.random.uniform(r, r+bin_width)

        # Return dimensionless r, implicitly in nanometers
        assert check_dimensionality(r, float)
        assert (r > 0)
        return r

    def _angle_log_pmf(self, angle, beta, n_divisions):
        """
        Calculate the log probability mass function (PMF) of drawing a angle.

        .. math ::

            p(\theta; \beta, K_\theta, \theta_0) \propto \sin(\theta) e^{-\frac{\beta K_\theta}{2} (\theta - \theta_0)^2 }

        Prameters
        ---------
        angle : parmed.Structure.Angle modified to use simtk.unit.Quantity
            Valence bond parameters
        beta : simtk.unit.Quantity with units compatible with 1/kilojoules_per_mole
            Inverse thermal energy
        n_divisions : int
            Number of quandrature points for drawing bond length

        Returns
        -------
        theta_i : np.ndarray of shape (n_divisions,) implicitly in units of radians
            theta_i[i] is the angle with corresponding log probability mass function p_i[i]
        log_p_i : np.ndarray of shape (n_divisions,)
            log_p_i[i] is the corresponding log probability mass of angle theta_i[i]
        bin_width : float implicitly in units of radians
            The bin width for individual PMF bins

        .. todo :: In future, this approach will be improved by eliminating discrete quadrature.

        """
        # TODO: Overhaul this method to accept unit-bearing quantities
        # TODO: Switch from simple discrete quadrature to more sophisticated computation of pdf

        # TODO: We end up computing the discretized PMF over and over again; we can speed this up by caching

        # Check input argument dimensions
        assert check_dimensionality(angle.type.theteq, unit.radians)
        assert check_dimensionality(angle.type.k, unit.kilojoules_per_mole/unit.radians**2)
        assert check_dimensionality(beta, unit.kilojoules_per_mole**(-1))

        # Retrieve relevant quantities for valence angle
        theta0 = angle.type.theteq
        k = angle.type.k * self._angle_softening_constant
        sigma_theta = unit.sqrt(1.0/(beta * k)) # standard deviation, unit-bearing quantity

        # Convert to dimensionless quantities in MD unit system
        theta0 = theta0.value_in_unit_system(unit.md_unit_system)
        k = k.value_in_unit_system(unit.md_unit_system)
        sigma_theta = sigma_theta.value_in_unit_system(unit.md_unit_system)

        # Determine integration bounds
        # We can't compute log(0) so we have to avoid sin(theta) = 0 near theta = {0, pi}
        EPSILON = 1.0e-3
        lower_bound, upper_bound = EPSILON, np.pi-EPSILON

        # Compute left bin edges
        theta_i, bin_width = np.linspace(lower_bound, upper_bound, num=n_divisions, retstep=True, endpoint=False)

        # Compute log probability
        from scipy.special import logsumexp
        log_p_i = np.log(np.sin(theta_i+(bin_width/2.0))) - 0.5*((theta_i+(bin_width/2.0)-theta0)/sigma_theta)**2
        log_p_i -= logsumexp(log_p_i)

        check_dimensionality(theta_i, float)
        check_dimensionality(log_p_i, float)
        check_dimensionality(bin_width, float)

        return theta_i, log_p_i, bin_width

    def _angle_logp(self, theta, angle, beta, n_divisions):
        """
        Calculate the log-probability of a given angle at a given inverse temperature

        Propose dimensionless bond length r from distribution

        .. math ::

            p(\theta; \beta, K_\theta, \theta_0) \propto \sin(\theta) e^{-\frac{\beta K_\theta}{2} (\theta - \theta_0)^2 }

        Prameters
        ---------
        theta : float
            angle, implicitly in radians
        angle : parmed.Structure.Angle modified to use simtk.unit.Quantity
            Valence angle parameters
        beta : simtk.unit.Quantity with units compatible with 1/kilojoules_per_mole
            Inverse thermal energy
        n_divisions : int
            Number of quandrature points for drawing angle

        .. todo :: In future, this approach will be improved by eliminating discrete quadrature.

        """
        # TODO: Overhaul this method to accept unit-bearing quantities
        # TODO: Switch from simple discrete quadrature to more sophisticated computation of pdf

        check_dimensionality(theta, float)
        check_dimensionality(beta, 1/unit.kilojoules_per_mole)

        theta_i, log_p_i, bin_width = self._angle_log_pmf(angle, beta, n_divisions)

        if (theta < theta_i[0]) or (theta >= theta_i[-1] + bin_width):
            return LOG_ZERO

        # Determine index that r falls within
        index = int((theta - theta_i[0]) / bin_width)
        assert (index >= 0) and (index < n_divisions)

        # Correct for division size
        logp = log_p_i[index] - np.log(bin_width)

        return logp

    def _propose_angle(self, angle, beta, n_divisions):
        """
        Propose dimensionless angle from distribution

        .. math ::

            \theta \sim p(\theta; \beta, K_\theta, \theta_0) \propto \sin(\theta) e^{-\frac{\beta K_\theta}{2} (\theta - \theta_0)^2 }

        Prameters
        ---------
        angle : parmed.Structure.Angle modified to use simtk.unit.Quantity
            Valence angle parameters
        beta : simtk.unit.Quantity with units compatible with 1/kilojoules_per_mole
            Inverse temperature
        n_divisions : int
            Number of quandrature points for drawing angle

        Returns
        -------
        theta : float
            Dimensionless valence angle, implicitly in radians

        .. todo :: In future, this approach will be improved by eliminating discrete quadrature.

        """
        # TODO: Overhaul this method to accept and return unit-bearing quantities
        # TODO: Switch from simple discrete quadrature to more sophisticated computation of pdf

        check_dimensionality(beta, 1/unit.kilojoules_per_mole)

        theta_i, log_p_i, bin_width = self._angle_log_pmf(angle, beta, n_divisions)

        # Draw an index
        index = np.random.choice(range(n_divisions), p=np.exp(log_p_i))
        theta = theta_i[index]

        # Draw uniformly in that bin
        theta = np.random.uniform(theta, theta+bin_width)

        # Return dimensionless theta, implicitly in nanometers
        assert check_dimensionality(theta, float)
        return theta

    def _torsion_scan(self, torsion_atom_indices, positions, r, theta, n_divisions):
        """
        Compute unit-bearing Carteisan positions and torsions (dimensionless, in md_unit_system) for a torsion scan

        Parameters
        ----------
        torsion_atom_indices : int tuple of shape (4,)
            Atom indices defining torsion, where torsion_atom_indices[0] is the atom to be driven
        positions : simtk.unit.Quantity of shape (natoms,3) with units compatible with nanometers
            Positions of the atoms in the system
        r : float (implicitly in md_unit_system)
            Dimensionless bond length (must be in nanometers)
        theta : float (implicitly in md_unit_system)
            Dimensionless valence angle (must be in radians)
        n_divisions : int
            The number of divisions for the torsion scan

        Returns
        -------
        xyzs : simtk.unit.Quantity wrapped np.ndarray of shape (n_divisions,3) with dimensions length
            The cartesian coordinates of each
        phis : np.ndarray of shape (n_divisions,), implicitly in radians
            The torsions angles representing the left bin edge at which a potential will be calculated
        bin_width : float, implicitly in radians
            The bin width of torsion scan increment

        """
        # TODO: Overhaul this method to accept and return unit-bearing quantities
        # TODO: Switch from simple discrete quadrature to more sophisticated computation of pdf

        assert check_dimensionality(positions, unit.angstroms)
        assert check_dimensionality(r, float)
        assert check_dimensionality(theta, float)

        # Compute dimensionless positions in md_unit_system as numba-friendly float64
        length_unit = unit.nanometers
        import copy
        positions_copy = copy.deepcopy(positions)
        positions_copy = positions_copy.value_in_unit(length_unit).astype(np.float64)
        atom_positions, bond_positions, angle_positions, torsion_positions = [ positions_copy[index] for index in torsion_atom_indices ]

        # Compute dimensionless torsion values for torsion scan
        phis, bin_width = np.linspace(-np.pi, +np.pi, num=n_divisions, retstep=True, endpoint=False)

        # Compute dimensionless positions for torsion scan
        from perses.rjmc import coordinate_numba
        internal_coordinates = np.array([r, theta, 0.0], np.float64)
        xyzs = coordinate_numba.torsion_scan(bond_positions, angle_positions, torsion_positions, internal_coordinates, phis)

        # Convert positions back into standard md_unit_system length units (nanometers)
        xyzs_quantity = unit.Quantity(xyzs, unit=unit.nanometers)

        # Return unit-bearing positions and dimensionless torsions (implicitly in md_unit_system)
        check_dimensionality(xyzs_quantity, unit.nanometers)
        check_dimensionality(phis, float)
        return xyzs_quantity, phis, bin_width

    def _torsion_log_pmf(self, growth_context, torsion_atom_indices, positions, r, theta, beta, n_divisions):
        """
        Calculate the torsion log probability using OpenMM, including all energetic contributions for the atom being driven

        This includes all contributions from bonds, angles, and torsions for the atom being placed
        (and, optionally, sterics if added to the growth system when it was created).

        Parameters
        ----------
        growth_context : simtk.openmm.Context
            Context containing the modified system
        torsion_atom_indices : int tuple of shape (4,)
            Atom indices defining torsion, where torsion_atom_indices[0] is the atom to be driven
        positions : simtk.unit.Quantity with shape (natoms,3) with units compatible with nanometers
            Positions of the atoms in the system
        r : float (implicitly in nanometers)
            Dimensionless bond length (must be in nanometers)
        theta : float (implcitly in radians on domain [0,+pi])
            Dimensionless valence angle (must be in radians)
        beta : simtk.unit.Quantity with units compatible with1/(kJ/mol)
            Inverse thermal energy
        n_divisions : int
            Number of divisions for the torsion scan

        Returns
        -------
        logp_torsions : np.ndarray of float with shape (n_divisions,)
            logp_torsions[i] is the normalized probability density at phis[i]
        phis : np.ndarray of float with shape (n_divisions,), implicitly in radians
            phis[i] is the torsion angle left bin edges at which the log probability logp_torsions[i] was calculated
        bin_width : float implicitly in radian
            The bin width for torsions

        .. todo :: In future, this approach will be improved by eliminating discrete quadrature.

        """
        # TODO: This method could benefit from memoization to speed up tests and particle filtering
        # TODO: Overhaul this method to accept and return unit-bearing quantities
        # TODO: Switch from simple discrete quadrature to more sophisticated computation of pdf

        check_dimensionality(positions, unit.angstroms)
        check_dimensionality(r, float)
        check_dimensionality(theta, float)
        check_dimensionality(beta, 1.0 / unit.kilojoules_per_mole)

        # Compute energies for all torsions
        logq = np.zeros(n_divisions) # logq[i] is the log unnormalized torsion probability density
        atom_idx = torsion_atom_indices[0]
        xyzs, phis, bin_width = self._torsion_scan(torsion_atom_indices, positions, r, theta, n_divisions)
        xyzs = xyzs.value_in_unit_system(unit.md_unit_system) # make positions dimensionless again
        positions = positions.value_in_unit_system(unit.md_unit_system)

        for i, xyz in enumerate(xyzs):
            # Set positions
            positions[atom_idx,:] = xyz
            growth_context.setPositions(positions)

            # Compute potential energy
            state = growth_context.getState(getEnergy=True)
            potential_energy = state.getPotentialEnergy()

            # Store unnormalized log probabilities
            logq_i = -beta*potential_energy
            logq[i] = logq_i

        # It's OK to have a few torsions with NaN energies,
        # but we need at least _some_ torsions to have finite energies
        if np.sum(np.isnan(logq)) == n_divisions:
            raise Exception("All %d torsion energies in torsion PMF are NaN." % n_divisions)

        # Suppress the contribution from any torsions with NaN energies
        logq[np.isnan(logq)] = -np.inf

        # Compute the normalized log probability
        from scipy.special import logsumexp
        logp_torsions = logq - logsumexp(logq)

        # Write proposed torsion energies to a PDB file for visualization or debugging, if desired
        if hasattr(self, '_proposal_pdbfile'):
            # Write proposal probabilities to PDB file as B-factors for inert atoms
            f_i = -logp_torsions
            f_i -= f_i.min() # minimum free energy is zero
            f_i[f_i > 999.99] = 999.99
            self._proposal_pdbfile.write('MODEL\n')
            for i, xyz in enumerate(xyzs):
                self._proposal_pdbfile.write('ATOM  %5d %4s %3s %c%4d    %8.3f%8.3f%8.3f%6.2f%6.2f\n' % (i+1, ' Ar ', 'Ar ', ' ', atom_idx+1, 10*xyz[0], 10*xyz[1], 10*xyz[2], np.exp(logp_torsions[i]), f_i[i]))
            self._proposal_pdbfile.write('TER\n')
            self._proposal_pdbfile.write('ENDMDL\n')
            # TODO: Write proposal PMFs to storage
            # atom_proposal_indices[order]
            # atom_positions[order,k]
            # torsion_pmf[order, division_index]

        assert check_dimensionality(logp_torsions, float)
        assert check_dimensionality(phis, float)
        assert check_dimensionality(bin_width, float)
        return logp_torsions, phis, bin_width

    def _propose_torsion(self, growth_context, torsion_atom_indices, positions, r, theta, beta, n_divisions):
        """
        Propose a torsion angle using OpenMM

        Parameters
        ----------
        growth_context : simtk.openmm.Context
            Context containing the modified system
        torsion_atom_indices : int tuple of shape (4,)
            Atom indices defining torsion, where torsion_atom_indices[0] is the atom to be driven
        positions : simtk.unit.Quantity with shape (natoms,3) with units compatible with nanometers
            Positions of the atoms in the system
        r : float (implicitly in nanometers)
            Dimensionless bond length (must be in nanometers)
        theta : float (implcitly in radians on domain [0,+pi])
            Dimensionless valence angle (must be in radians)
        beta : simtk.unit.Quantity with units compatible with1/(kJ/mol)
            Inverse thermal energy
        n_divisions : int
            Number of divisions for the torsion scan

        Returns
        -------
        phi : float, implicitly in radians
            The proposed torsion angle
        logp : float
            The log probability of the proposal

        .. todo :: In future, this approach will be improved by eliminating discrete quadrature.

        """
        # TODO: Overhaul this method to accept and return unit-bearing quantities
        # TODO: Switch from simple discrete quadrature to more sophisticated computation of pdf

        check_dimensionality(positions, unit.angstroms)
        check_dimensionality(r, float)
        check_dimensionality(theta, float)
        check_dimensionality(beta, 1.0 / unit.kilojoules_per_mole)

        # Compute probability mass function for all possible proposed torsions
        logp_torsions, phis, bin_width = self._torsion_log_pmf(growth_context, torsion_atom_indices, positions, r, theta, beta, n_divisions)

        # Draw a torsion bin and a torsion uniformly within that bin
        index = np.random.choice(range(len(phis)), p=np.exp(logp_torsions))
        phi = phis[index]
        logp = logp_torsions[index]

        # Draw uniformly within the bin
        phi = np.random.uniform(phi, phi+bin_width)
        logp -= np.log(bin_width)

        assert check_dimensionality(phi, float)
        assert check_dimensionality(logp, float)
        return phi, logp

    def _torsion_logp(self, growth_context, torsion_atom_indices, positions, r, theta, phi, beta, n_divisions):
        """
        Calculate the logp of a torsion using OpenMM

        Parameters
        ----------
        growth_context : simtk.openmm.Context
            Context containing the modified system
        torsion_atom_indices : int tuple of shape (4,)
            Atom indices defining torsion, where torsion_atom_indices[0] is the atom to be driven
        positions : simtk.unit.Quantity with shape (natoms,3) with units compatible with nanometers
            Positions of the atoms in the system
        r : float (implicitly in nanometers)
            Dimensionless bond length (must be in nanometers)
        theta : float (implicitly in radians on domain [0,+pi])
            Dimensionless valence angle (must be in radians)
        phi : float (implicitly in radians on domain [-pi,+pi))
            Dimensionless torsion angle (must be in radians)
        beta : simtk.unit.Quantity with units compatible with1/(kJ/mol)
            Inverse thermal energy
        n_divisions : int
            Number of divisions for the torsion scan

        Returns
        -------
        torsion_logp : float
            The log probability this torsion would be drawn
        """
        # TODO: Overhaul this method to accept and return unit-bearing quantities

        # Check that quantities are unitless
        check_dimensionality(positions, unit.angstroms)
        check_dimensionality(r, float)
        check_dimensionality(theta, float)
        check_dimensionality(phi, float)
        check_dimensionality(beta, 1.0 / unit.kilojoules_per_mole)

        # Compute torsion probability mass function
        logp_torsions, phis, bin_width = self._torsion_log_pmf(growth_context, torsion_atom_indices, positions, r, theta, beta, n_divisions)

        # Determine which bin the torsion falls within
        index = np.argmin(np.abs(phi-phis)) # WARNING: This assumes both phi and phis have domain of [-pi,+pi)

        # Convert from probability mass function to probability density function so that sum(dphi*p) = 1, with dphi = (2*pi)/n_divisions.
        torsion_logp = logp_torsions[index] - np.log(bin_width)

        assert check_dimensionality(torsion_logp, float)
        return torsion_logp

class GeometrySystemGenerator(object):
    """
    Internal utility class to generate OpenMM systems with only valence terms and special parameters for newly placed atoms to assist in geometry proposals.

    The resulting system will have the specified global context parameter (controlled by ``parameter_name``)
    that selects which proposed atom will have all its valence terms activated. When this parameter is set to the
    index of the atom being added within ``growth_indices``, all valence terms associated with that atom will be computed.
    Only valence terms involving newly placed atoms will be computed; valence terms between fixed atoms will be omitted.
    """

    def __init__(self, reference_system, torsion_proposal_order, global_parameter_name='growth_index', add_extra_torsions = True, add_extra_angles = True, connectivity_graph = None,
                       reference_graph=None, use_sterics=False, force_names=None, force_parameters=None, verbose=True, neglect_angles = True, use_14_nonbondeds = True):
        """
        Parameters
        ----------
        reference_system : simtk.openmm.System object
            The system containing the relevant forces and particles
        torsion_proposal_order : list of list of 4-int
            The order in which the torsion indices will be proposed
        global_parameter_name : str, optional, default='growth_index'
            The name of the global context parameter
        add_extra_torsions : bool, default False
            Whether to add additional torsions to keep rings flat. Default False.
        add_extra_angles : bool, default False
            Whether to add additional angles to keep rings flat.  Default False
        connectivity_graph : nx.Graph, default None
            graph to specify connectivity
        reference_graph : nx.Graph, default None
            NetworkXProposalOrder._residue_graph with ring perception
        use_sterics: bool, default False
            whether to use nonbonded interactions to place atoms
        force_names : list of str
            A list of the names of forces that will be included in this system
        force_parameters : dict
            Options for the forces (e.g., NonbondedMethod : 'CutffNonPeriodic')
        neglect_angles : bool
            whether to ignore and report on theta angle potentials that add variance to the work
        verbose : bool, optional, default True
            If True, will print verbose output.
        neglect_angles : bool
            whether to neglect (coupled) angle terms that would make the variance non-zero (within numerical tolerance threshold)
        use_14_nonbondeds : bool, default True
            whether to consider 1,4 exception interactions in the geometry proposal


        Attributes
        ----------
        growth_system : simtk.openmm.System object
            The system containing all of the valence forces to be added (with the exception of neglected angle forces if neglect_angles == False) with respect
            to the reference_system Parameter.
        atoms_with_positions_system : simtk.openmm.System object
            The system containing all of the core atom valence forces.  This is to be used in the proposal to assert that the final growth_system energy plus
            the atoms_with_positions_system energy is equal to the final_system energy (for the purpose of energy bookkeeping).
        neglected_angle_terms : list of ints
            The indices of the HarmonicAngleForce parameters which are neglected for the purpose of minimizing work variance.  This will be empty if neglect_angles == False.
        """
        import copy
        import networkx as nx
        # TODO: Rename `growth_indices` (which is really a list of Atom objects) to `atom_growth_order` or `atom_addition_order`

        # Check that we're not using the reserved name
        if global_parameter_name == 'growth_idx':
            raise ValueError('global_parameter_name cannot be "growth_idx" due to naming collisions')

        growth_indices = [ torsion[0] for torsion in torsion_proposal_order ]
        default_growth_index = len(growth_indices) # default value of growth index to use in System that is returned
        self.current_growth_index = default_growth_index
        self.special_terms = {'omitted_bonds': [], 'omitted_angles': [], 'omitted_torsions': [], 'omitted_1,4s': [], 'extra_torsions': [], 'extra_angles': []}

        #we need to create new-to-core bonds and new-new bonds:

        self.connectivity_graph = copy.deepcopy(connectivity_graph)
        self.reference_graph = copy.deepcopy(reference_graph)
        self._index_to_node = {atom.index: atom for atom in self.reference_graph.nodes}
        self.omitted_edges = [(edge[0].index, edge[1].index) for edge in self.reference_graph.edges if not self.connectivity_graph.has_edge(edge[0].index, edge[1].index)]
        _logger.info(f"\tomitted edges: {self.omitted_edges}")

        # Bonds, angles, and torsions
        self._HarmonicBondForceEnergy = "select(step({}+0.1 - growth_idx), (K/2)*(r-r0)^2, 0);"
        self._HarmonicAngleForceEnergy = "select(step({}+0.1 - growth_idx), (K/2)*(theta-theta0)^2, 0);"
        self._PeriodicTorsionForceEnergy = "select(step({}+0.1 - growth_idx), k*(1+cos(periodicity*theta-phase)), 0);"

        # Nonbonded sterics and electrostatics.
        # TODO: Allow user to select whether electrostatics or sterics components are included in the nonbonded interaction energy.
        self._nonbondedEnergy = "select(step({}+0.1 - growth_idx), U_sterics + U_electrostatics, 0);"
        self._nonbondedEnergy += "growth_idx = max(growth_idx1, growth_idx2);"
        # Sterics
        from openmmtools.constants import ONE_4PI_EPS0 # OpenMM constant for Coulomb interactions (implicitly in md_unit_system units)
        # TODO: Auto-detect combining rules to allow this to work with other force fields?
        # TODO: Enable more flexible handling / metaprogramming of CustomForce objects?
        self._nonbondedEnergy += "U_sterics = 4*epsilon*x*(x-1.0); x = (sigma/r)^6;"
        self._nonbondedEnergy += "epsilon = sqrt(epsilon1*epsilon2); sigma = 0.5*(sigma1 + sigma2);"
        # Electrostatics
        self._nonbondedEnergy += "U_electrostatics = ONE_4PI_EPS0*charge1*charge2/r;"
        self._nonbondedEnergy += "ONE_4PI_EPS0 = %f;" % ONE_4PI_EPS0

        # Exceptions
        self._nonbondedExceptionEnergy = "select(step({}+0.1 - growth_idx), U_exception, 0);"
        self._nonbondedExceptionEnergy += "U_exception = ONE_4PI_EPS0*chargeprod/r + 4*epsilon*x*(x-1.0); x = (sigma/r)^6;"
        self._nonbondedExceptionEnergy += "ONE_4PI_EPS0 = %f;" % ONE_4PI_EPS0

        self.sterics_cutoff_distance = 9.0 * unit.angstroms # cutoff for steric interactions with added/deleted atoms

        self.verbose = verbose

        # Get list of particle indices for new and old atoms.
        new_particle_indices = growth_indices
        old_particle_indices = [idx for idx in range(reference_system.getNumParticles()) if idx not in new_particle_indices]

        # Compile index of reference forces
        reference_forces = dict()
        reference_forces_indices = dict()
        for (index, force) in enumerate(reference_system.getForces()):
            force_name = force.__class__.__name__
            if force_name in reference_forces:
                raise ValueError('reference_system has two {} objects. This is currently unsupported.'.format(force_name))
            else:
                reference_forces_indices[force_name] = index
                reference_forces[force_name] = force

        # Create new System
        from simtk import openmm
        growth_system = openmm.System()
        atoms_with_positions_system = copy.deepcopy(reference_system)

        # Copy particles
        for i in range(reference_system.getNumParticles()):
            growth_system.addParticle(reference_system.getParticleMass(i))


        # Virtual sites are, in principle, automatically supported

        # Create bond force
        _logger.info("\tcreating bond force...")
        self.modified_bond_force = openmm.CustomBondForce(self._HarmonicBondForceEnergy.format(global_parameter_name))
        self.modified_bond_force.addGlobalParameter(global_parameter_name, default_growth_index)
        for parameter_name in ['r0', 'K', 'growth_idx']:
            self.modified_bond_force.addPerBondParameter(parameter_name)
        growth_system.addForce(self.modified_bond_force)
        reference_bond_force = reference_forces['HarmonicBondForce']
        _logger.info(f"\tthere are {reference_bond_force.getNumBonds()} bonds in reference force.")
        for bond_index in range(reference_bond_force.getNumBonds()):
            p1, p2, r0, K = reference_bond_force.getBondParameters(bond_index)
            growth_idx = self._calculate_growth_idx([p1, p2], growth_indices)
            _logger.debug(f"\t\tfor bond {bond_index} (i.e. partices {p1} and {p2}), the growth_index is {growth_idx}")
            if growth_idx > 0:
                hypothetical_edge = (p1, p2)
                if self.connectivity_graph.has_edge(*hypothetical_edge):
                    self.modified_bond_force.addBond(p1, p2, [r0, K, growth_idx])
                    _logger.debug(f"\t\t\tadding to the growth system")
                else:
                    self.special_terms['omitted_bonds'].append((p1, p2))
                atoms_with_positions_system.getForce(reference_forces_indices['HarmonicBondForce']).setBondParameters(bond_index,p1, p2, r0, K*0.0)
            else:
                _logger.debug(f"\t\t\tadding to the the atoms with positions system.")

        # Create angle force
        # NOTE: here, we are implementing an angle exclusion scheme for angle terms that are coupled to lnZ_phi
        _logger.info("\tcreating angle force...")
        self.modified_angle_force = openmm.CustomAngleForce(self._HarmonicAngleForceEnergy.format(global_parameter_name))
        self.modified_angle_force.addGlobalParameter(global_parameter_name, default_growth_index)
        for parameter_name in ['theta0', 'K', 'growth_idx']:
            self.modified_angle_force.addPerAngleParameter(parameter_name)
        growth_system.addForce(self.modified_angle_force)
        reference_angle_force = reference_forces['HarmonicAngleForce']
        neglected_angle_term_indices = [] #initialize the index list of neglected angle forces
        _logger.info(f"\tthere are {reference_angle_force.getNumAngles()} angles in reference force.")
        for angle in range(reference_angle_force.getNumAngles()):
            p1, p2, p3, theta0, K = reference_angle_force.getAngleParameters(angle)
            growth_idx = self._calculate_growth_idx([p1, p2, p3], growth_indices)
            _logger.debug(f"\t\tfor angle {angle} (i.e. partices {p1}, {p2}, and {p3}), the growth_index is {growth_idx}")

            if growth_idx > 0:
                if neglect_angles and (not use_sterics):
                    if any( [p1, p2, p3] == torsion[:3] or [p3,p2,p1] == torsion[:3] for torsion in torsion_proposal_order):
                        #then there is a new atom in the angle term and the angle is part of a torsion and is necessary
                        _logger.debug(f"\t\t\tadding to the growth system since it is part of a torsion")
                        self.modified_angle_force.addAngle(p1, p2, p3, [theta0, K, growth_idx])
                    else:
                        #then it is a neglected angle force, so it must be tallied
                        _logger.debug(f"\t\t\ttallying to neglected term indices")
                        neglected_angle_term_indices.append(angle)
                else:
                    _logger.debug(f"\t\t\tadding to the growth system")
                    _logger.debug(f"\t\t\tall simple paths between {p1} and {p3}: {list(nx.algorithms.simple_paths.all_simple_paths(self.connectivity_graph, p1, p3, cutoff=2))}")
                    #_simple_paths = list(nx.algorithms.simple_paths.all_simple_paths(self.connectivity_graph, p1, p3, cutoff=2))
                    #if any(([p1,p2,p3] == path or [p3,p2,p1] == path) for path in _simple_paths):
                    _possible_edges = [(p1,p2),(p2,p3),(p2,p1),(p3,p2)]
                    if any(_edge in self.omitted_edges for _edge in _possible_edges):
                        self.special_terms['omitted_angles'].append((p1, p2, p3))
                        _logger.debug(f"\t\t\tadding to omitted angles")
                    else:
                        _logger.debug(f"\t\t\tadding to the growth system")
                        self.modified_angle_force.addAngle(p1, p2, p3, [theta0, K, growth_idx])

                atoms_with_positions_system.getForce(reference_forces_indices['HarmonicAngleForce']).setAngleParameters(angle, p1, p2, p3, theta0, K*0.0)
            else:
                #then it is an angle term of core atoms and should be added to the atoms_with_positions_angle_force
                _logger.debug(f"\t\t\tadding to the the atoms with positions system.")

        # Create torsion force
        _logger.info("\tcreating torsion force...")
        self.modified_torsion_force = openmm.CustomTorsionForce(self._PeriodicTorsionForceEnergy.format(global_parameter_name))
        self.modified_torsion_force.addGlobalParameter(global_parameter_name, default_growth_index)
        for parameter_name in ['periodicity', 'phase', 'k', 'growth_idx']:
            self.modified_torsion_force.addPerTorsionParameter(parameter_name)
        growth_system.addForce(self.modified_torsion_force)
        reference_torsion_force = reference_forces['PeriodicTorsionForce']
        _logger.info(f"\tthere are {reference_torsion_force.getNumTorsions()} torsions in reference force.")
        for torsion in range(reference_torsion_force.getNumTorsions()):
            p1, p2, p3, p4, periodicity, phase, k = reference_torsion_force.getTorsionParameters(torsion)
            growth_idx = self._calculate_growth_idx([p1, p2, p3, p4], growth_indices)
            _logger.debug(f"\t\tfor torsion {torsion} (i.e. partices {p1}, {p2}, {p3}, and {p4}), the growth_index is {growth_idx}")
            if growth_idx > 0:
                #_logger.debug(f"\t\tall simple paths between {p1} and {p4}: {list(nx.algorithms.simple_paths.all_simple_paths(self.connectivity_graph, p1, p4, cutoff=3))}")
                #_simple_paths = list(nx.algorithms.simple_paths.all_simple_paths(self.connectivity_graph, p1, p4, cutoff=3))
                _possible_edges = [(p1,p2),(p2,p3),(p3,p4)]
                if any((_edge in self.omitted_edges) or (_edge[::-1] in self.omitted_edges) for _edge in _possible_edges):
                    self.special_terms['omitted_torsions'].append((p1,p2,p3,p4))
                    _logger.debug(f"\t\t\tadding to omitted torsions")
                else:
                    _logger.debug(f"\t\t\tadding to the growth system")
                    self.modified_torsion_force.addTorsion(p1, p2, p3, p4, [periodicity, phase, k, growth_idx])
                atoms_with_positions_system.getForce(reference_forces_indices['PeriodicTorsionForce']).setTorsionParameters(torsion, p1, p2, p3, p4, periodicity, phase, k*0.0)
            else:
                _logger.debug(f"\t\t\tadding to the the atoms with positions system.")

        # TODO: check this for bugs by turning on sterics
        if (use_sterics or use_14_nonbondeds) and 'NonbondedForce' in reference_forces.keys():
            _logger.info("\tcreating nonbonded force...")

            # Copy parameters for local sterics parameters in nonbonded force
            reference_nonbonded_force = reference_forces['NonbondedForce']
            atoms_with_positions_system.getForce(reference_forces_indices['NonbondedForce']).setUseDispersionCorrection(False)

            _logger.info("\t\tgrabbing reference nonbonded method, cutoff, switching function, switching distance...")
            reference_nonbonded_force_method = reference_nonbonded_force.getNonbondedMethod()
            _logger.debug(f"\t\t\tnonbonded method: {reference_nonbonded_force_method}")
            reference_nonbonded_force_cutoff = reference_nonbonded_force.getCutoffDistance()
            _logger.debug(f"\t\t\tnonbonded cutoff distance: {reference_nonbonded_force_cutoff}")
            reference_nonbonded_force_switching_function = reference_nonbonded_force.getUseSwitchingFunction()
            _logger.debug(f"\t\t\tnonbonded switching function (boolean): {reference_nonbonded_force_switching_function}")
            reference_nonbonded_force_switching_distance = reference_nonbonded_force.getSwitchingDistance()
            _logger.debug(f"\t\t\tnonbonded switching distance: {reference_nonbonded_force_switching_distance}")

            #now we add the 1,4 interaction force
            if reference_nonbonded_force.getNumExceptions() > 0:
                _logger.info("\t\tcreating nonbonded exception force (i.e. custom bond for 1,4s)...")
                custom_bond_force = openmm.CustomBondForce(self._nonbondedExceptionEnergy.format(global_parameter_name))
                custom_bond_force.addGlobalParameter(global_parameter_name, default_growth_index)
                for parameter_name in ['chargeprod', 'sigma', 'epsilon', 'growth_idx']:
                    custom_bond_force.addPerBondParameter(parameter_name)
                growth_system.addForce(custom_bond_force)

                #Now we iterate through the exceptions and add custom bond forces if the growth intex for that bond > 0
                _logger.info("\t\tlooping through exceptions calculating growth indices, and adding appropriate interactions to custom bond force.")
                _logger.info(f"\t\tthere are {reference_nonbonded_force.getNumExceptions()} in the reference Nonbonded force")
                for exception_index in range(reference_nonbonded_force.getNumExceptions()):
                    p1, p2, chargeprod, sigma, epsilon = reference_nonbonded_force.getExceptionParameters(exception_index)
                    growth_idx = self._calculate_growth_idx([p1, p2], growth_indices)
                    _logger.debug(f"\t\t\t{p1} and {p2} with charge {chargeprod} and epsilon {epsilon} have a growth index of {growth_idx}")
                    # Only need to add terms that are nonzero and involve newly added atoms.
                    if (growth_idx > 0) and ((chargeprod.value_in_unit_system(unit.md_unit_system) != 0.0) or (epsilon.value_in_unit_system(unit.md_unit_system) != 0.0)):
                        _logger.debug(f"\t\t\tis considered exception...")
                        _possible_edges = list(nx.algorithms.simple_paths.all_simple_paths(self.connectivity_graph, p1, p2, cutoff=3))
                        _connectivity_edges = [[(e[0], e[1]), (e[1], e[2]), (e[2], e[3])] for e in _possible_edges]
                        _logger.debug(f"\t\t\t\tconnectivity_edges: {_connectivity_edges}")
                        commit_to_omitted = False
                        if not _connectivity_edges: #then we add to the omitted terms
                            commit_to_omitted = True
                            _logger.debug(f"\t\t\t\tcommitting to omitted 1,4s")
                            self.special_terms['omitted_1,4s'].append((p1, p2))
                        else:
                            for _edge in _connectivity_edges:
                                if any((i in self.omitted_edges) or (i[::-1] in self.omitted_edges) for i in _edge):
                                    commit_to_omitted = True
                                    _logger.debug(f"\t\t\t\tcommitting to omitted 1,4s")
                                    self.special_terms['omitted_1,4s'].append((p1, p2))
                                    break

                        if not commit_to_omitted:
                            _logger.debug(f"\t\t\t\tcommitting to custom bond")
                            custom_bond_force.addBond(p1, p2, [chargeprod, sigma, epsilon, growth_idx])

            else:
                _logger.info("\t\tthere are no Exceptions in the reference system.")

            if use_sterics:
                #now we define a custom nonbonded force for the growth system
                _logger.info("\t\tadding custom nonbonded force...")
                modified_sterics_force = openmm.CustomNonbondedForce(self._nonbondedEnergy.format(global_parameter_name))
                modified_sterics_force.addGlobalParameter(global_parameter_name, default_growth_index)
                for parameter_name in ['charge', 'sigma', 'epsilon', 'growth_idx']:
                    modified_sterics_force.addPerParticleParameter(parameter_name)
                growth_system.addForce(modified_sterics_force)

                # Translate nonbonded method to the custom nonbonded force
                import simtk.openmm.app as app
                _logger.info("\t\tsetting nonbonded method, cutoff, switching function, and switching distance to custom nonbonded force...")
                if reference_nonbonded_force_method in [0,1]: #if Nonbonded method is NoCutoff or CutoffNonPeriodic
                    modified_sterics_force.setNonbondedMethod(reference_nonbonded_force_method)
                    modified_sterics_force.setCutoffDistance(reference_nonbonded_force_cutoff)
                elif reference_nonbonded_force_method in [2,3,4]:
                    modified_sterics_force.setNonbondedMethod(2)
                    modified_sterics_force.setCutoffDistance(self.sterics_cutoff_distance)
                    modified_sterics_force.setUseSwitchingFunction(reference_nonbonded_force_switching_function)
                    modified_sterics_force.setSwitchingDistance(reference_nonbonded_force_switching_distance)
                else:
                    raise Exception(f"reference force nonbonded method {reference_nonbonded_force_method} is NOT supported for custom nonbonded force!")

                # define atoms_with_positions_Nonbonded_Force
                #atoms_with_positions_nonbonded_force.setUseDispersionCorrection(False)

                # Add particle parameters to the custom nonbonded force...and add interactions to the atoms_with_positions_nonbonded_force if growth_index == 0
                _logger.info("\t\tlooping through reference nonbonded force to add particle params to custom nonbonded force")
                for particle_index in range(reference_nonbonded_force.getNumParticles()):
                    [charge, sigma, epsilon] = reference_nonbonded_force.getParticleParameters(particle_index)
                    growth_idx = self._calculate_growth_idx([particle_index], growth_indices)
                    modified_sterics_force.addParticle([charge, sigma, epsilon, growth_idx])
                    if particle_index in growth_indices:
                        atoms_with_positions_system.getForce(reference_forces_indices['NonbondedForce']).setParticleParameters(particle_index, charge*0.0, sigma, epsilon*0.0)

                # Add exclusions, which are active at all times.
                # (1,4) exceptions are always included, since they are part of the valence terms.
                _logger.info("\t\tlooping through reference nonbonded force exceptions to add exclusions to custom nonbonded force")
                for exception_index in range(reference_nonbonded_force.getNumExceptions()):
                    [p1, p2, chargeprod, sigma, epsilon] = reference_nonbonded_force.getExceptionParameters(exception_index)
                    modified_sterics_force.addExclusion(p1, p2)

                    #we also have to add the exceptions to the atoms_with_positions_nonbonded_force
                    #if len(set([p1, p2]).intersection(set(old_particle_indices))) == 2:
                    if len(set([p1,p2]).intersection(set(growth_indices))) > 0:
                        _logger.debug(f"\t\t\tparticle {p1} and/or {p2}  are new indices and have an exception of {chargeprod} and {epsilon}.  setting to zero.")
                        #then both particles are old, so we can add the exception to the atoms_with_positions_nonbonded_force
                        atoms_with_positions_system.getForce(reference_forces_indices['NonbondedForce']).setExceptionParameters(exception_index, p1, p2, chargeprod * 0.0, sigma, epsilon * 0.0)


                # Only compute interactions of new particles with all other particles
                # TODO: Allow inteactions to be resticted to only the residue being grown.
                modified_sterics_force.addInteractionGroup(set(new_particle_indices), set(old_particle_indices))
                modified_sterics_force.addInteractionGroup(set(new_particle_indices), set(new_particle_indices))

                if reference_nonbonded_force_method in [0,1]:
                    if 'MonteCarloBarostat' in reference_forces_indices.keys():
                        atoms_with_positions_system.removeForce(reference_forces_indices['MonteCarloBarostat'])

            else:
                if 'MonteCarloBarostat' in reference_forces_indices.keys():
                    atoms_with_positions_system.removeForce(reference_forces_indices['MonteCarloBarostat'])
                if 'NonbondedForce' in reference_forces_indices.keys(): #if we aren't using 14 interactions, we simply delete the nonbonded force object
                    atoms_with_positions_system.removeForce(reference_forces_indices['NonbondedForce'])
        elif 'NonbondedForce' in reference_forces.keys():
            if 'MonteCarloBarostat' in reference_forces_indices.keys():
                atoms_with_positions_system.removeForce(reference_forces_indices['MonteCarloBarostat'])
            if 'NonbondedForce' in reference_forces_indices.keys(): #if we aren't using 14 interactions, we simply delete the nonbonded force object
                atoms_with_positions_system.removeForce(reference_forces_indices['NonbondedForce'])



        # Add extra ring-closing torsions, if requested.  This can also be called if we are making proposals involving chiral centers or bond stereochemistry
        # In this case, the biasing torsions will be annealed off for unique new atoms and on for unique old atoms.
        # If we are conducting full annealing of stereoisomers, then there are no biasing torsions proposed here;
        #       instead, annealing will be conducted entirely over the annealing protocol.
        _logger.info("\tchecking for extra torsions forces...")
        self._determine_extra_terms(reference_graph, torsion_proposal_order, add_extra_torsions, add_extra_angles)

        # Store growth system
        self._growth_parameter_name = global_parameter_name
        self._growth_system = growth_system
        self._atoms_with_positions_system = atoms_with_positions_system #note this is only bond, angle, and torsion forces
        self.neglected_angle_terms = neglected_angle_term_indices #these are angle terms that are neglected because of coupling to lnZ_phi
        _logger.info("Neglected angle terms : {}".format(neglected_angle_term_indices))

    def set_growth_parameter_index(self, growth_parameter_index, context=None):
        """
        Set the growth parameter index
        """
        # TODO: Set default force global parameters if context is not None.
        if context is not None:
            context.setParameter(self._growth_parameter_name, growth_parameter_index)
        self.current_growth_index = growth_parameter_index

    def get_modified_system(self):
        """
        Create a modified system with parameter_name parameter. When 0, only core atoms are interacting;
        for each integer above 0, an additional atom is made interacting, with order determined by growth_index.

        Returns
        -------
        growth_system : simtk.openmm.System object
            System with the appropriate modifications, with growth parameter set to maximum.
        """
        return self._growth_system

    def _determine_extra_terms(self, reference_graph, torsion_proposal_order, add_extra_torsions = True, add_extra_angles = True):
        """
        In order to facilitate ring closure and ensure proper chirality/bond stereochemistry,
        we add additional biasing torsions to rings and stereobonds that are then corrected
        for in the acceptance probability.

        Determine which residue is covered by the new atoms
        Identify rotatable bonds
        Construct analogous residue in OpenEye and generate configurations with Omega
        Measure appropriate torsions and generate relevant parameters

        .. warning :: Only one residue should be changing

        .. warning :: This currently will not work for polymer residues

        .. todo :: Use a database of biasing torsions constructed ahead of time and match to residues by NetworkX

        Parameters
        ----------
        reference_graph : NetworkX.Graph
            the new/old graph if forward/backward
        torsion_proposal_order : list of list of 4-ints
            the torsion terms that are proposed in order
        add_extra_torsions : bool, default True
            whether to allow the addition of biasing torsions
        add_extra_angles : bool, default True
            whether to allow the addition of biasing angles
        """
        # Do nothing if there are no atoms to grow.
        _logger.debug(f"\treference graph nodes:{reference_graph.nodes}")
        growth_indices = [torsion[0] for torsion in torsion_proposal_order]
        if len(growth_indices) == 0:
            return

        #now, for each torsion, extract the set of indices and the angle
        periodicity = 1
        phase = np.pi*unit.radians
        k = 120.0*unit.kilocalories_per_mole # stddev of 12 degrees
        for growth_idx, torsion in enumerate(torsion_proposal_order):
            _logger.debug(f"\t\titerating through torsion: {torsion}")
            #to determine if all atoms in the torsion term are part of the same ring structure
            ring_membership = [set(self.reference_graph.nodes[self._index_to_node[i]]['cycle_membership']) for i in torsion]
            _logger.debug(f"\t\tring membership: {ring_membership}")
            if len(ring_membership[0].intersection(ring_membership[1], ring_membership[2], ring_membership[3])) != 0:
                _logger.debug(f"\t\tall torsion terms ({torsion}) are in the same ring")
                #then each member of the torsion is in the same ring and we should make the torsion eclipsed
                if add_extra_torsions:
                    _logger.debug(f"\t\tadding {torsion} to extra torsions")
                    self.modified_torsion_force.addTorsion(*torsion, [periodicity, phase, k, growth_idx])
                    self.special_terms['extra_torsions'].append([*torsion, [periodicity, phase, k, growth_idx]])


    def _calculate_growth_idx(self, particle_indices, growth_indices):
        """
        Utility function to calculate the growth index of a particular force.
        For each particle index, it will check to see if it is in growth_indices.
        If not, 0 is added to an array, if yes, the index in growth_indices is added.
        Finally, the method returns the max of the accumulated array
        Parameters
        ----------
        particle_indices : list of int
            The indices of particles involved in this force
        growth_indices : list of int
            The ordered list of indices for atom position proposals
        Returns
        -------
        growth_idx : int
            The growth_idx parameter
        """
        particle_indices_set = set(particle_indices)
        growth_indices_set = set(growth_indices)
        new_atoms_in_force = particle_indices_set.intersection(growth_indices_set)
        if len(new_atoms_in_force) == 0:
            return 0
        new_atom_growth_order = [growth_indices.index(atom_idx)+1 for atom_idx in new_atoms_in_force]
        return max(new_atom_growth_order)

class NetworkXProposalOrder(object):
    """
    This is a proposal order generating object that uses just networkx and graph traversal for simplicity.
    """

    def __init__(self, topology_proposal, direction="forward"):
        """
        Create a NetworkXProposalOrder class
        Parameters
        ----------
        topology_proposal : perses.rjmc.topology_proposal.TopologyProposal
            Container class for the transformation
        direction: str, default forward
            Whether to go forward or in reverse for the proposal.
        """
        _logger.debug(f"\tinstantiating NetworkXProposalOrder...")
        from simtk.openmm import app
        import copy

        self._topology_proposal = topology_proposal
        self._direction = direction
        self._hydrogen = app.Element.getByAtomicNumber(1.0)

        # Set the direction
        if direction == "forward":
            self._destination_system = self._topology_proposal.new_system
            self._new_atoms = self._topology_proposal.unique_new_atoms
            self._destination_topology = self._topology_proposal.new_topology
            self._atoms_with_positions = self._topology_proposal.new_to_old_atom_map.keys()
        elif direction == "reverse":
            self._destination_system = self._topology_proposal.old_system
            self._new_atoms = self._topology_proposal.unique_old_atoms
            self._destination_topology = self._topology_proposal.old_topology
            self._atoms_with_positions = self._topology_proposal.old_to_new_atom_map.keys()
        else:
            raise ValueError("Direction must be either forward or reverse.")

        self._new_atom_objects = list(self._destination_topology.atoms())
        self._new_atoms_to_place = [atom for atom in self._destination_topology.atoms() if atom.index in self._new_atoms]

        self._atoms_with_positions_set = set(self._atoms_with_positions)

        self._hydrogens = []
        self._heavy = []

        # Sort the new atoms into hydrogen and heavy atoms:
        for atom in self._new_atoms_to_place:
            if atom.element == self._hydrogen:
                self._hydrogens.append(atom.index)
            else:
                self._heavy.append(atom.index)

        _logger.debug(f"\theavy atoms to be placed: {self._heavy}")
        _logger.debug(f"\thydrogens to be placed: {self._hydrogens}")

        # Sanity check
        if len(self._hydrogens)==0 and len(self._heavy)==0:
            msg = 'NetworkXProposalOrder: No new atoms for direction {}\n'.format(direction)
            msg += str(topology_proposal)
            raise Exception(msg)

        # Choose the first of the new atoms to find the corresponding residue:
        self.transforming_residue = self._new_atom_objects[self._new_atoms[0]].residue

        _logger.debug(f"\tcreating residue graph...")
        self._residue_graph = self._residue_to_graph()
        self._index_to_node = {atom.index: atom for atom in self._residue_graph.nodes}
        #_logger.debug(f"\tresidue graph nodes: {self._residue_graph.nodes}")
        _logger.debug(f"\tperceiving ring structure")
        self._perceive_ring_structures()
        _logger.debug(f"\tresidue graph ring memberships: {[atom.index for atom in self._residue_graph.nodes]}")

        #now to define a connectivity graph for proper torsion proposal
        self._connectivity_graph = nx.Graph()
        for node in self._residue_graph.nodes:
            if node.index not in self._new_atoms:
                self._connectivity_graph.add_node(node.index)

        for edge in self._residue_graph.edges:
            _edge = [a.index for a in edge]
            if not set(self._new_atoms).intersection(set(_edge)):
                self._connectivity_graph.add_edge(*_edge)
        _logger.debug(f"\tconnectivity graph: {self._connectivity_graph}")
        self._residue_atoms_with_positions_set = set(atom for atom in self._atoms_with_positions if atom in self._index_to_node.keys())

    def determine_proposal_order(self):
        """
        Determine the proposal order of this system pair.
        This includes the choice of a torsion. As such, a logp is returned.

        Parameters
        ----------
        direction : str, optional
            whether to determine the forward or reverse proposal order

        Returns
        -------
        atom_torsions : list of list of int
            A list of torsions, where the first atom in the torsion is the one being proposed
        logp_torsion_choice : list
            log probability of the chosen torsions as a list of sequential atom placements
        """
        _logger.debug(f"\tdetermining proposal order for heavy atoms...")
        heavy_atoms_torsions, heavy_logp = self._propose_atoms_in_order(self._heavy)
        _logger.debug(f"\tdetermining proposal order for hydrogen atoms...")
        hydrogen_atoms_torsions, hydrogen_logp = self._propose_atoms_in_order(self._hydrogens)
        proposal_order = heavy_atoms_torsions + hydrogen_atoms_torsions

        if len(proposal_order) == 0:
            msg = 'NetworkXProposalOrder: proposal_order is empty\n'
            raise Exception(msg)

        #Check that no atom is placed until each atom in the corresponding torsion is in the set of atoms with positions
        _set_of_atoms_with_positions = set(self._atoms_with_positions)

        # Now iterate through the proposal_order, ensuring that each atom in the corresponding torsion list is in the _set_of_atoms_with_positions (appending to the set after each placement)
        for torsion in proposal_order:
            assert set(torsion[1:]).issubset(_set_of_atoms_with_positions), "Proposal Order Issue: a torsion atom is not position-defined"
            _set_of_atoms_with_positions.add(torsion[0])

        # Ensure lists are not ill-defined
        assert heavy_logp + hydrogen_logp != [], "logp list of log_probabilities from torsion choices is an empty list"
        assert len(heavy_logp + hydrogen_logp) == len(proposal_order), "There is a mismatch in the size of the atom torsion proposals and the associated logps"

        return proposal_order, heavy_logp + hydrogen_logp

    def _propose_atoms_in_order(self, atom_group):
        """
        Propose a group of atoms along with corresponding torsions and a total log probability for the choice
        Parameters
        ----------
        atom_group : list of int
            The atoms to propose

        Returns
        -------
        atom_torsions : list of list of int
            A list of torsions, where the atom_torsions[0] is the one being proposed
        logp : list
            The contribution to the overall proposal log probability as a list of sequential logps

        """
        _logger.debug(f"\t\tproposing atoms in order...")
        from scipy import special
        atom_torsions= []
        logp = []
        assert len(atom_group) == len(set(atom_group)), "There are duplicate atom indices in the list of atom proposal indices"
        atom_list = list(self.transforming_residue.atoms())
        _logger.debug(f"\t\tatom group: {atom_group}")
        #_logger.debug(f"\t\tatom_list: {atom_list}")
        _logger.debug(f"\t\tatoms with positions: {self._atoms_with_positions_set}")
        _logger.debug(f"\t\tresidue atoms with positions: {self._residue_atoms_with_positions_set}")

        #we will check for cycle closures: if this is the case, the proposal criteria are biased
        #current proposal:
        #   iterate per usual; if atom encountered is a member of ring i, then we continue to place members of ring i until finished
        while len(atom_group) > 0:
            _logger.debug(f"\t\t\tatom group: {atom_group}; length: {len(atom_group)}")
            eligible_torsions_list = self.produce_eligible_torsions_list(atom_group)
            _logger.debug(f"eligible torsions: {eligible_torsions_list}")
            assert len(eligible_torsions_list) != 0, "There is a connectivity issue; there are no torsions from which to choose"
            ntorsions = len(eligible_torsions_list)
            random_torsion_index = np.random.choice(range(ntorsions))
            random_torsion = eligible_torsions_list[random_torsion_index]
            _logger.debug(f"\t\t\trandom torsion chosen: {random_torsion}; chosen atom: {random_torsion[0]}")
            chosen_atom_index = random_torsion[0]
            atom_torsions.append(random_torsion)
            atom_group.remove(chosen_atom_index)
            self._atoms_with_positions_set.add(chosen_atom_index)
            self._residue_atoms_with_positions_set.add(chosen_atom_index)
            _logger.debug(f"\t\t\tresidue atoms with positions: {self._residue_atoms_with_positions_set}")
            logp.append(np.log(1./ntorsions))

            #add the atom and bond connectivty to the connectivity graph
            self.add_torsion_connectivity(random_torsion)

            cycle_membership = self._residue_graph.nodes[self._index_to_node[chosen_atom_index]]['cycle_membership']
            _logger.debug(f"\t\t\tcycle membership: {cycle_membership}")
            #if the cycle membership is not empty, pull each entry and check if there are atoms in the atom_group that are part of this
            #ring; if this is true, and the number of atoms placed in this ring is >=3, then we close the ring with torsion that are only ring-members
            if cycle_membership != []: #then we iterate through each ring associated with the atom
                _logger.debug(f"\t\t\t cycle membership is not empty...proceeding to close ring")
                #we will determine the number of unplaced atoms in each ring associated with this atom:
                unplaced_atoms = {cycle: [atom_idx for atom_idx in atom_group if cycle in self._residue_graph.nodes[self._index_to_node[atom_idx]]['cycle_membership']] for cycle in cycle_membership}
                _logger.debug(f"\t\t\tunplaced atoms: {unplaced_atoms}")
                for cycle in unplaced_atoms.keys():
                    _logger.debug(f"\t\t\t\t cycle of interest: {cycle}")
                    cycle_atom_group = unplaced_atoms[cycle]
                    _logger.debug(f"\t\t\t\tunplaced atoms in cycle {cycle}: {cycle_atom_group}")
                    placed_atoms_in_cycle = [atom_idx for atom_idx in self._residue_atoms_with_positions_set if cycle in self._residue_graph.nodes[self._index_to_node[atom_idx]]['cycle_membership']]
                    _logger.debug(f"\t\t\t\tplaced atoms in cycle {cycle}: {placed_atoms_in_cycle}")
                    if len(cycle_atom_group) > 0 and len(placed_atoms_in_cycle) > 2:
                        _logger.debug(f"\t\t\t\tthere is at least one unplaced atoms in the cycle and at least 3 placed atoms in the cycle...")
                        # since there are unplaced atoms in this cycle and there are at least 3 atoms already placed, we can place the rest of the atoms in the cycle
                        # based on torsions that are specific to the cycle; this is advantageous because later, we can enumerate in-cycle torsions and ensure that they
                        # are always gauche or puckered
                        while len(cycle_atom_group) > 0:
                            eligible_torsions_list = self.produce_eligible_torsions_list(cycle_atom_group, ring_closure = True)
                            _logger.debug(f"\t\t\t\t\tunfiltered torsions list: {eligible_torsions_list}")
                            filtered_eligible_torsions_list = [torsion for torsion in eligible_torsions_list if (set(torsion[1:]).issubset(set(placed_atoms_in_cycle))) or (set(torsion[:3]).issubset(set(placed_atoms_in_cycle)))]
                            _logger.debug(f"\t\t\t\t\teligible cycle torsions list: {filtered_eligible_torsions_list}")
                            if filtered_eligible_torsions_list == []:
                                raise Exception(f"there are no eligible torsions in the ring proposal")
                            ntorsions = len(filtered_eligible_torsions_list)
                            random_torsion_index = np.random.choice(range(ntorsions))
                            random_torsion = filtered_eligible_torsions_list[random_torsion_index]
                            _logger.debug(f"\t\t\t\t\trandom torsions chosen: {random_torsion}; chosen atom: {random_torsion[0]}")
                            chosen_atom_index = random_torsion[0]
                            atom_torsions.append(random_torsion)
                            atom_group.remove(chosen_atom_index)
                            cycle_atom_group.remove(chosen_atom_index)
                            placed_atoms_in_cycle.append(chosen_atom_index)
                            self._atoms_with_positions_set.add(chosen_atom_index)
                            self._residue_atoms_with_positions_set.add(chosen_atom_index)
                            logp.append(np.log(1./ntorsions))
                            #add the atom and bond connectivty to the connectivity graph
                            self.add_torsion_connectivity(random_torsion)
                        _logger.debug(f"\t\t\t\tcycle atom group for cycle {cycle} is complete")
                _logger.debug(f"\t\t\tplaced all atoms in cycles")

        return atom_torsions, logp

    def add_torsion_connectivity(self, torsion):
        #given a 4-tuple indexed torsion, we add the torsion bonds and new atoms to the self._connectivity graph
        #we need this to track connectivity
        self._connectivity_graph.add_node(torsion[0])
        for pair in [(torsion[0], torsion[1]), (torsion[1], torsion[2]), (torsion[2], torsion[3])]:
            residue_graph_pair = (self._index_to_node[pair[0]], self._index_to_node[pair[1]])
            if self._residue_graph.has_edge(*residue_graph_pair): #if the edge is defined in the self._residue_graph, we can add
                #this filters out instances where torsions are defined over atoms without bond connectivity
                self._connectivity_graph.add_edge(*pair)

    def produce_eligible_torsions_list(self, atom_group, ring_closure = False):
        eligible_torsions_list = []
        for atom_index in atom_group:
            atom = self._index_to_node[atom_index]
            # Find the shortest path up to length four from the atom in question:
            if not ring_closure:
                shortest_paths = nx.algorithms.single_source_shortest_path(self._residue_graph, atom, cutoff=3)
            else:
                atom_ring = self._residue_graph.nodes[atom]['cycle_membership']
                possible_destinations = [atom for atom in self._residue_graph.nodes if len(set(self._residue_graph.nodes[atom]['cycle_membership']).intersection(set(atom_ring))) > 0 and atom.index in self._atoms_with_positions_set]
                _logger.debug(f"\t\t\t\t\tpossible destinations: {[atom.index for atom in possible_destinations]}")
                shortest_paths_unflat = [list(nx.algorithms.simple_paths.all_simple_paths(self._residue_graph, atom, pd)) for pd in possible_destinations]
                shortest_paths_flat = [item for sublist in shortest_paths_unflat for item in sublist]
                shortest_paths = {i[-1]: i for i in shortest_paths_flat if len(i) == 4}

            # Loop through the destination and path of each path and append to eligible_torsions_list
            # if destination has a position and path[1:3] is a subset of atoms with positions
            for destination, path in shortest_paths.items():
                index_path = [q.index for q in path]
                _logger.debug(f"\t\t\t\t\t\tpotential torsion index path: {index_path}")
                # Check if the path is length 4 (a torsion) and that the destination has a position. Continue if not.
                if len(index_path) != 4 or destination.index not in self._atoms_with_positions_set:
                    _logger.debug(f"\t\t\t\t\t\t{index_path} omitted (not length of 4 or not in atoms_with_positions)")
                    continue

                index_pairs = [(index_path[1], index_path[2]), (index_path[2], index_path[3])]
                if any(not self._connectivity_graph.has_edge(*pair) for pair in index_pairs):
                    _logger.debug(f"\t\t\t\t\t\t{index_path} omitted (in an unspecified bond)")
                    continue

                # If the last atom is in atoms with positions, check to see if the others are also.
                # If they are, append the torsion to the list of possible torsions to propose
                if set(index_path[1:]).issubset(self._atoms_with_positions_set):
                    _logger.debug(f"\t\t\t\t\t\t{index_path} added to eligible_torsions_list")
                    eligible_torsions_list.append(index_path)

        return eligible_torsions_list

    def _residue_to_graph(self):
        """
        Create a NetworkX graph representing the connectivity of a residue

        Returns
        -------
        residue_graph : nx.Graph
            A graph representation of the residue
        """
        g = nx.Graph()
        residue = self.transforming_residue

        for atom in residue.atoms():
            g.add_node(atom)

        for bond in residue.bonds():
            g.add_edge(bond[0], bond[1])

        return g

    def _perceive_ring_structures(self):
        """
        Adds attributes to each node that enumerates which cycle it is in.
        """
        cycles = nx.cycle_basis(self._residue_graph)
        _logger.debug(f"\t\tcycles: {cycles}")
        for node in self._residue_graph.nodes:
            cycle = []
            for index, _cycle in enumerate(cycles):
                if node in _cycle:
                    cycle.append(index)
            self._residue_graph.nodes[node]['cycle_membership'] = cycle

class NoTorsionError(Exception):
    def __init__(self, message):
        # Call the base class constructor with the parameters it needs
        super(NoTorsionError, self).__init__(message)
