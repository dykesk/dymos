from __future__ import division, print_function, absolute_import

from collections import Iterable

import numpy as np
from dymos.phases.components import EndpointConditionsComp
from dymos.phases.phase_base import PhaseBase, _unspecified
from dymos.phases.grid_data import GridData

from openmdao.api import IndepVarComp, Group, ParallelGroup, NonlinearRunOnce, NonlinearBlockJac, \
    NonlinearBlockGS, NewtonSolver
from six import iteritems

from .solvers.nl_rk_solver import NonlinearRK
from .components.segment.explicit_segment import ExplicitSegment
from .components.implicit_segment_connection_comp import ImplicitSegmentConnectionComp
from ...utils.rk_methods import rk_methods
from ...utils.misc import CoerceDesvar, get_rate_units
from ...utils.constants import INF_BOUND


class ExplicitPhase(PhaseBase):
    """
    ExplicitPhase provides explicit time stepping multiple shooting phases.

    Attributes
    ----------
    self.time_options : dict of TimeOptionsDictionary
        A dictionary of options for time (integration variable) in the phase.

    self.state_options : dict of StateOptionsDictionary
        A dictionary of options for the RHS states in the Phase.

    self.control_options : dict of ControlOptionsDictionary
        A dictionary of options for the controls in the Phase.

    self._ode_controls : dict of ControlOptionsDictionary
        A dictionary of the default options for controllable inputs of the Phase RHS

    """
    def __init__(self, num_segments, transcription_order=3, num_steps=10, segment_ends=None,
                 compressed=True, **kwargs):
        kwgs = kwargs.copy()
        kwgs.update({'num_segments': num_segments, 'transcription_order': transcription_order,
                    'segment_ends': segment_ends, 'num_steps': num_steps, 'compressed': compressed})

        super(ExplicitPhase, self).__init__(**kwgs)

        # Pluck out the kwargs needed to initialize grid_data, potentially needed prior to setup.
        num_segments = num_segments
        transcription_order = transcription_order
        segment_ends = segment_ends
        compressed = compressed

        self.grid_data = GridData(num_segments=num_segments, transcription='explicit',
                                  transcription_order=transcription_order,
                                  num_steps_per_segment=num_steps, segment_ends=segment_ends,
                                  compressed=compressed)

    def initialize(self):
        super(ExplicitPhase, self).initialize()
        self.options['transcription'] = 'explicit'
        self.options.declare('num_steps', default=10, types=(int, Iterable),
                             desc='Number of steps to take within each segment.')
        self.options.declare('method', default='rk4', values=('rk4',),
                             desc='The integrator used within the explicit phase.')
        self.options.declare('shooting', default='single', values=('single', 'multiple', 'hybrid'),
                             desc='The shooting method used to integrate across the phase.  Single'
                                  'shooting propagates the state from segment to segment, '
                                  'serially.  Multiple shooting runs each segment in parallel and'
                                  'uses an optimizer to enforce state continuity at segment bounds.'
                                  ' Hybrid propagates the segments in parallel but enforces state '
                                  'continuity with a nonlinear solver.')
        self.options.declare('seg_solver_class', default=NonlinearRK,
                             values=(NonlinearRK, NonlinearBlockGS, NewtonSolver),
                             desc='The nonlinear solver class used to converge the numerical '
                                  'integration of the segment.')

    def setup(self):
        super(ExplicitPhase, self).setup()

        num_opt_controls = len([name for (name, options) in iteritems(self.control_options)
                                if options['opt']])

        num_controls = len(self.control_options)

        indep_controls = ['indep_controls'] if num_opt_controls > 0 else []
        design_params = ['design_params'] if self.design_parameter_options else []
        input_params = ['input_params'] if self.input_parameter_options else []
        control_interp_comp = ['control_interp_comp'] if num_controls > 0 else []

        order = self._time_extents + indep_controls + \
            input_params + design_params + \
            ['indep_states', 'time'] + control_interp_comp

        order = order + ['segments', 'indep_jumps', 'initial_conditions', 'final_conditions']

        if self._initial_boundary_constraints:
            order.append('initial_boundary_constraints')
        if self._final_boundary_constraints:
            order.append('final_boundary_constraints')
        if self._path_constraints:
            order.append('path_constraints')
        self.set_order(order)

    def _setup_time(self):
        comps = super(ExplicitPhase, self)._setup_time()
        gd = self.grid_data

        for iseg in range(gd.num_segments):
            i1, i2 = gd.subset_segment_indices['all'][iseg, :]
            seg_idxs = gd.subset_node_indices['all'][i1:i2]
            seg_end_idxs = seg_idxs[[0, -1]]
            self.connect('time', 'seg_{0}.seg_t0_tf'.format(iseg),
                         src_indices=seg_end_idxs)
        return comps

    def _setup_rhs(self):
        gd = self.grid_data
        shooting = self.options['shooting']

        if shooting in ('single', 'hybrid'):
            group_class = Group
        else:
            group_class = ParallelGroup

        segments_group = self.add_subsystem('segments', subsys=group_class(),
                                            promotes_inputs=['*'], promotes_outputs=['*'])

        if shooting in ('single', 'multiple'):
            segments_group.nonlinear_solver = NonlinearRunOnce()
        elif shooting == 'hybrid':
            segments_group.nonlinear_solver = NonlinearBlockJac()

        for iseg in range(gd.num_segments):

            # Add a segment connector for each segment pair if in 'single' or 'hybrid' shooting
            if iseg > 0 and shooting in ('single', 'hybrid'):
                seg_connect_i = ImplicitSegmentConnectionComp(state_options=self.state_options)
                con_name = 'connect_{0}_{1}'.format(iseg - 1, iseg)
                segments_group.add_subsystem(con_name,
                                             subsys=seg_connect_i)

                for state_name, options in iteritems(self.state_options):
                    shape = options['shape']
                    size = int(np.prod(shape))
                    lhs_src_idxs = np.arange(-size, 0, dtype=int).reshape(shape)

                    self.connect('seg_{0}.step_states:{1}'.format(iseg - 1, state_name),
                                 '{0}.lhs_states:{1}'.format(con_name, state_name),
                                 src_indices=lhs_src_idxs)

                    self.connect('{0}.rhs_states:{1}'.format(con_name, state_name),
                                 'seg_{0}.initial_states:{1}'.format(iseg, state_name))

            # Add the segment
            segment_i = ExplicitSegment(index=iseg,
                                        grid_data=self.grid_data,
                                        num_steps=self.options['num_steps'],
                                        method='rk4',
                                        ode_class=self.options['ode_class'],
                                        ode_init_kwargs=self.options['ode_init_kwargs'],
                                        time_options=self.time_options,
                                        state_options=self.state_options,
                                        control_options=self.control_options,
                                        design_parameter_options=self.design_parameter_options,
                                        input_parameter_options=self.input_parameter_options,
                                        seg_solver_class=self.options['seg_solver_class'])

            segments_group.add_subsystem('seg_{0}'.format(iseg),
                                         subsys=segment_i)

    def _setup_states(self):
        """
        Add an IndepVarComp for the states and setup the states as design variables.
        """
        gd = self.grid_data
        num_state_input_nodes = gd.subset_num_nodes['state_input']
        shooting = self.options['shooting']

        indep = IndepVarComp()
        for name, options in iteritems(self.state_options):
            indep.add_output(name='states:{0}'.format(name),
                             shape=(num_state_input_nodes, np.prod(options['shape'])),
                             units=options['units'])

            for iseg in range(gd.num_segments):
                self.connect('states:{0}'.format(name),
                             'seg_{0}.initial_states:{1}'.format(iseg, name),
                             src_indices=gd.subset_node_indices['state_disc'][iseg])
                if shooting in ('single', 'hybrid'):
                    break

        self.add_subsystem('indep_states', indep, promotes_outputs=['*'])

        # Add the initial state values as design variables, if necessary

        for name, options in iteritems(self.state_options):
            size = np.prod(options['shape'])
            if options['opt']:
                desvar_indices = list(range(size * num_state_input_nodes))

                if options['fix_initial']:
                    if options['initial_bounds'] is not None:
                        raise ValueError('Cannot specify \'fix_initial=True\' and specify '
                                         'initial_bounds for state {0}'.format(name))
                    if isinstance(options['fix_initial'], Iterable):
                        idxs_to_fix = np.where(np.asarray(options['fix_initial']))[0]
                        for idx_to_fix in reversed(sorted(idxs_to_fix)):
                            del desvar_indices[idx_to_fix]
                    else:
                        del desvar_indices[:size]

                if len(desvar_indices) > 0:
                    coerce_desvar_option = CoerceDesvar(num_state_input_nodes, desvar_indices,
                                                        options)

                    lb = np.zeros_like(desvar_indices, dtype=float)
                    lb[:] = -INF_BOUND if coerce_desvar_option('lower') is None else \
                        coerce_desvar_option('lower')

                    ub = np.zeros_like(desvar_indices, dtype=float)
                    ub[:] = INF_BOUND if coerce_desvar_option('upper') is None else \
                        coerce_desvar_option('upper')

                    if options['initial_bounds'] is not None:
                        lb[0] = options['initial_bounds'][0]
                        ub[0] = options['initial_bounds'][-1]

                    self.add_design_var(name='states:{0}'.format(name),
                                        lower=lb,
                                        upper=ub,
                                        scaler=coerce_desvar_option('scaler'),
                                        adder=coerce_desvar_option('adder'),
                                        ref0=coerce_desvar_option('ref0'),
                                        ref=coerce_desvar_option('ref'),
                                        indices=desvar_indices)

    def _setup_controls(self):
        super(ExplicitPhase, self)._setup_controls()
        gd = self.grid_data
        for name, options in iteritems(self.control_options):
            for iseg in range(gd.num_segments):
                i1, i2 = gd.subset_segment_indices['control_disc'][iseg]
                self.connect('control_interp_comp.control_values:{0}'.format(name),
                             'seg_{0}.disc_controls:{1}'.format(iseg, name),
                             src_indices=np.arange(i1, i2, dtype=int))

    def _setup_defects(self):
        """
        Setup the Collocation and Continuity components as necessary.
        """
        pass

    def _setup_endpoint_conditions(self):
        num_seg = self.grid_data.num_segments

        jump_comp = self.add_subsystem('indep_jumps', subsys=IndepVarComp(),
                                       promotes_outputs=['*'])

        jump_comp.add_output('initial_jump:time', val=0.0, units=self.time_options['units'],
                             desc='discontinuity in time at the start of the phase')

        jump_comp.add_output('final_jump:time', val=0.0, units=self.time_options['units'],
                             desc='discontinuity in time at the end of the phase')

        ic_comp = EndpointConditionsComp(loc='initial',
                                         time_options=self.time_options,
                                         state_options=self.state_options,
                                         control_options=self.control_options)

        self.add_subsystem(name='initial_conditions', subsys=ic_comp, promotes_outputs=['*'])

        fc_comp = EndpointConditionsComp(loc='final',
                                         time_options=self.time_options,
                                         state_options=self.state_options,
                                         control_options=self.control_options)

        self.add_subsystem(name='final_conditions', subsys=fc_comp, promotes_outputs=['*'])

        self.connect('time', 'initial_conditions.initial_value:time')
        self.connect('time', 'final_conditions.final_value:time')

        self.connect('initial_jump:time',
                     'initial_conditions.initial_jump:time')

        self.connect('final_jump:time',
                     'final_conditions.final_jump:time')

        for state_name, options in iteritems(self.state_options):
            size = np.prod(options['shape'])
            ar = np.arange(size)

            jump_comp.add_output('initial_jump:{0}'.format(state_name),
                                 val=np.zeros(options['shape']),
                                 units=options['units'],
                                 desc='discontinuity in {0} at the '
                                      'start of the phase'.format(state_name))

            jump_comp.add_output('final_jump:{0}'.format(state_name),
                                 val=np.zeros(options['shape']),
                                 units=options['units'],
                                 desc='discontinuity in {0} at the '
                                      'end of the phase'.format(state_name))

            self.connect('seg_0.step_states:{0}'.format(state_name),
                         'initial_conditions.initial_value:{0}'.format(state_name))
            self.connect('seg_{0}.step_states:{1}'.format(num_seg - 1, state_name),
                         'final_conditions.final_value:{0}'.format(state_name))

            self.connect('initial_jump:{0}'.format(state_name),
                         'initial_conditions.initial_jump:{0}'.format(state_name),
                         src_indices=ar, flat_src_indices=True)

            self.connect('final_jump:{0}'.format(state_name),
                         'final_conditions.final_jump:{0}'.format(state_name),
                         src_indices=ar, flat_src_indices=True)

        for control_name, options in iteritems(self.control_options):
            size = np.prod(options['shape'])
            ar = np.arange(size)

            jump_comp.add_output('initial_jump:{0}'.format(control_name),
                                 val=np.zeros(options['shape']),
                                 units=options['units'],
                                 desc='discontinuity in {0} at the '
                                      'start of the phase'.format(control_name))

            jump_comp.add_output('final_jump:{0}'.format(control_name),
                                 val=np.zeros(options['shape']),
                                 units=options['units'],
                                 desc='discontinuity in {0} at the '
                                      'end of the phase'.format(control_name))

            self.connect('control_interp_comp.control_values:{0}'.format(control_name),
                         'initial_conditions.initial_value:{0}'.format(control_name))

            self.connect('control_interp_comp.control_values:{0}'.format(control_name),
                         'final_conditions.final_value:{0}'.format(control_name))

            self.connect('initial_jump:{0}'.format(control_name),
                         'initial_conditions.initial_jump:{0}'.format(control_name),
                         src_indices=ar, flat_src_indices=True)

            self.connect('final_jump:{0}'.format(control_name),
                         'final_conditions.final_jump:{0}'.format(control_name),
                         src_indices=ar, flat_src_indices=True)

    def _setup_path_constraints(self):
        pass

    def _get_parameter_connections(self, name):
        """
        Returns a list containing tuples of each path and related indices to which the
        given design variable name is to be connected.

        Returns
        -------
        connection_info : list of (paths, indices)
            A list containing a tuple of target paths and corresponding src_indices to which the
            given design variable is to be connected.
        """
        connection_info = []
        template = 'seg_{0}.stage_ode.{1}'
        num_stages = rk_methods[self.options['method']]['num_stages']

        if name in self.ode_options._parameters:
            ode_tgts = self.ode_options._parameters[name]['targets']

            for i in range(self.grid_data.num_segments):
                num_steps = self.grid_data.num_steps_per_segment[i]
                num_nodes = num_stages * num_steps
                src_idxs = [0] * num_nodes
                connection_info.append(([template.format(i, t) for t in ode_tgts], src_idxs))

        return connection_info

    def set_state_options(self, name, units=_unspecified, val=1.0,
                          fix_initial=False, fix_final=False, initial_bounds=None,
                          final_bounds=None, lower=None, upper=None, scaler=None, adder=None,
                          ref=None, ref0=None, defect_scaler=1.0, defect_ref=None):
        """
        Set options that apply the EOM state variable of the given name.

        Parameters
        ----------
        name : str
            Name of the state variable in the RHS.
        units : str or None
            Units in which the state variable is defined.  Internally components may use different
            units for the state variable, but the IndepVarComp which provides its value will provide
            it in these units, and collocation defects will use these units.  If units is not
            specified here then the value as defined in the ODEOptions (@declare_state) will be
            used.
        val :  ndarray
            The default value of the state at the state discretization nodes of the phase.
        fix_initial : bool(False)
            If True, omit the first value of the state from the design variables (prevent the
            optimizer from changing it).
        fix_final : bool(False)
            If True, omit the final value of the state from the design variables (prevent the
            optimizer from changing it).
        lower : float or ndarray or None (None)
            The lower bound of the state at the nodes of the phase.
        upper : float or ndarray or None (None)
            The upper bound of the state at the nodes of the phase.
        scaler : float or ndarray or None (None)
            The scaler of the state value at the nodes of the phase.
        adder : float or ndarray or None (None)
            The adder of the state value at the nodes of the phase.
        ref0 : float or ndarray or None (None)
            The zero-reference value of the state at the nodes of the phase.
        ref : float or ndarray or None (None)
            The unit-reference value of the state at the nodes of the phase
        defect_scaler : float or ndarray (1.0)
            The scaler of the state defect at the collocation nodes of the phase.
        defect_ref : float or ndarray (1.0)
            The unit-reference value of the state defect at the collocation nodes of the phase. If
            provided, this value overrides defect_scaler.

        """
        if fix_final:
            raise ValueError('fix_final is not a valid option for states in ExplicitPhase. '
                             'Use a final boundary constraint on the state instead.')

        if units is not _unspecified:
            self.state_options[name]['units'] = units

        self.state_options[name]['val'] = val
        self.state_options[name]['fix_initial'] = fix_initial
        self.state_options[name]['fix_final'] = False
        self.state_options[name]['initial_bounds'] = initial_bounds
        self.state_options[name]['final_bounds'] = final_bounds
        self.state_options[name]['lower'] = lower
        self.state_options[name]['upper'] = upper
        self.state_options[name]['scaler'] = scaler
        self.state_options[name]['adder'] = adder
        self.state_options[name]['ref'] = ref
        self.state_options[name]['ref0'] = ref0
        self.state_options[name]['defect_scaler'] = defect_scaler
        self.state_options[name]['defect_ref'] = defect_ref

    def add_objective(self, name, loc='final', index=None, shape=(1,), ref=None, ref0=None,
                      adder=None, scaler=None, parallel_deriv_color=None,
                      vectorize_derivs=False):
        """
        Allows the user to add an objective in the phase.  If name is not a state,
        control, control rate, or 'time', then this is assumed to be the path of the variable
        to be constrained in the RHS.

        Parameters
        ----------
        name : str
            Name of the objective variable.  This should be one of 'time', a state or control
            variable, or the path to an output from the top level of the RHS.
        loc : str
            Where in the phase the objective is to be evaluated.  Valid
            options are 'initial' and 'final'.  The default is 'final'.
        index : int, optional
            If variable is an array at each point in time, this indicates which index is to be
            used as the objective, assuming C-ordered flattening.
        shape : int, optional
            The shape of the objective variable, at a point in time
        ref : float or ndarray, optional
            Value of response variable that scales to 1.0 in the driver.
        ref0 : float or ndarray, optional
            Value of response variable that scales to 0.0 in the driver.
        adder : float or ndarray, optional
            Value to add to the model value to get the scaled value. Adder
            is first in precedence.
        scaler : float or ndarray, optional
            value to multiply the model value to get the scaled value. Scaler
            is second in precedence.
        parallel_deriv_color : string
            If specified, this design var will be grouped for parallel derivative
            calculations with other variables sharing the same parallel_deriv_color.
        vectorize_derivs : bool
            If True, vectorize derivative calculations.
        """
        var_type = self._classify_var(name)
        gd = self.grid_data
        num_seg = gd.num_segments

        # Determine the path to the variable
        if var_type == 'time':
            obj_path = 'time'
        elif var_type == 'state':
            if loc == 'initial':
                obj_path = 'seg_{0}.step_states:{1}'.format(0, name)
            else:
                obj_path = 'seg_{0}.step_states:{1}'.format(num_seg - 1, name)
        elif var_type == 'indep_control':
            obj_path = 'control_interp_comp.control_values:{0}'.format(name)
        elif var_type == 'input_control':
            obj_path = 'control_interp_comp.control_values:{0}'.format(name)
        elif var_type == 'control_rate':
            control_name = name[:-5]
            obj_path = 'control_rates:{0}_rate'.format(control_name)
        elif var_type == 'control_rate2':
            control_name = name[:-6]
            obj_path = 'control_rates:{0}_rate2'.format(control_name)
        elif var_type == 'design_parameter':
            obj_path = 'design_parameters:{0}'.format(name)
        elif var_type == 'input_parameter':
            obj_path = 'input_parameters:{0}_out'.format(name)
        else:
            # Failed to find variable, assume it is in the RHS
            if loc == 'initial':
                obj_path = 'seg_{0}.stage_ode.{1}'.format(0, name)
            else:
                obj_path = 'seg_{0}.stage_ode.{1}'.format(num_seg - 1, name)

        super(ExplicitPhase, self)._add_objective(obj_path, loc=loc, index=index, shape=shape,
                                                  ref=ref, ref0=ref0, adder=adder,
                                                  scaler=scaler,
                                                  parallel_deriv_color=parallel_deriv_color,
                                                  vectorize_derivs=vectorize_derivs)

    def _get_boundary_constraint_src(self, var, loc):
        # Determine the path to the variable which we will be constraining
        time_units = self.time_options['units']
        var_type = self._classify_var(var)

        src_seg = 'seg_{0}'.format(0 if loc == 'initial' else self.grid_data.num_segments - 1)

        if var_type == 'time':
            shape = (1,)
            units = self.time_units
            linear = True
            constraint_path = '{0}.t_step'.format(src_seg)
        elif var_type == 'state':
            state_shape = self.state_options[var]['shape']
            state_units = self.state_options[var]['units']
            shape = state_shape
            units = state_units
            linear = True if loc == 'initial' else False
            constraint_path = '{0}.step_states:{1}'.format(src_seg, var)
        elif var_type in 'indep_control':
            control_shape = self.control_options[var]['shape']
            control_units = self.control_options[var]['units']
            shape = control_shape
            units = control_units
            linear = True
            constraint_path = 'control_values:{0}'.format(var)
        elif var_type == 'input_control':
            control_shape = self.control_options[var]['shape']
            control_units = self.control_options[var]['units']
            shape = control_shape
            units = control_units
            linear = False
            constraint_path = 'control_values:{0}'.format(var)
        elif var_type == 'design_parameter':
            control_shape = self.design_parameter_options[var]['shape']
            control_units = self.design_parameter_options[var]['units']
            shape = control_shape
            units = control_units
            linear = True
            constraint_path = 'design_parameters:{0}'.format(var)
        elif var_type == 'input_parameter':
            control_shape = self.input_parameter_options[var]['shape']
            control_units = self.input_parameter_options[var]['units']
            shape = control_shape
            units = control_units
            linear = False
            constraint_path = 'input_parameters:{0}_out'.format(var)
        elif var_type == 'control_rate':
            control_var = var[:-5]
            control_shape = self.control_options[control_var]['shape']
            control_units = self.control_options[control_var]['units']
            control_rate_units = get_rate_units(control_units, time_units, deriv=1)
            shape = control_shape
            units = control_rate_units
            linear = False
            constraint_path = 'control_rates:{0}'.format(var)
        elif var_type == 'control_rate2':
            control_var = var[:-6]
            control_shape = self.control_options[control_var]['shape']
            control_units = self.control_options[control_var]['units']
            control_rate_units = get_rate_units(control_units, time_units, deriv=2)
            shape = control_shape
            units = control_rate_units
            linear = False
            constraint_path = 'control_rates:{0}'.format(var)
        else:
            # Failed to find variable, assume it is in the RHS
            print(var)
            constraint_path = '{0}.stage_ode.{1}'.format(src_seg, var)
            shape = None
            units = None
            linear = False

        return constraint_path, shape, units, linear

    # def get_values(self, var, nodes='solution', units=None):
    #     """
    #     Retrieve the values of the given variable at the given
    #     subset of nodes.
    #
    #     Parameters
    #     ----------
    #     var : str
    #         The variable whose values are to be returned.  This may be
    #         the name 'time', the name of a state, control, or parameter,
    #         or the path to a variable in the ODEFunction of the phase.
    #     nodes : str
    #         The name of the node subset.
    #     units : str
    #         The units in which the values should be expressed.  Must be compatible
    #         with the corresponding units inside the phase.
    #
    #     Returns
    #     -------
    #     ndarray
    #         An array of the values at the requested node subset.  The
    #         node index is the first dimension of the ndarray.
    #     """
    #     gd = self.grid_data
    #     num_stages = rk_methods[self.options['method']]['s']
    #
    #     var_type = self._classify_var(var)
    #
    #     outputs = dict(self.list_outputs(explicit=True, values=True, units=True,
    # shape=True, out_stream=None))
    #     inputs = dict(self.list_inputs(values=True, units=True, out_stream=None))
    #
    #     # print('\n'.join([o for o in outputs if 'states:y' in o]))
    #
    #     if units is not None:
    #         if not valid_units(units):
    #             raise ValueError('Units {0} is not a valid units identifier'.format(units))
    #
    #     path = '{0}.'.format(self.pathname) if self.pathname else ''
    #
    #     path_map = {'time': 'time.{0}',
    #                 'state': 'indep_states.states:{0}',
    #                 'indep_control': 'control_interp_comp.control_values:{0}',
    #                 'input_control': 'control_interp_comp.control_values:{0}',
    #                 'design_parameter': 'design_params.design_parameters:{0}',
    #                 'input_parameter': 'input_params.input_parameters:{0}_out',
    #                 'control_rate': 'control_interp_comp.control_rates:{0}',
    #                 'control_rate2': 'control_interp_comp.control_rates:{0}',
    #                 'ode': 'rhs_all.{0}'}
    #
    #     if var_type == 'state':
    #         var_path = path + path_map[var_type].format(var)
    #
    #         output_value = np.nan * np.ones(gd.subset_num_nodes['all'])
    #
    #         tmp = path + 'segments.seg_{0}.step_{1}.ycomp_stage_{2}.states:{3}_{2}'
    #
    #         # Populate the stage values
    #         stage_values = [[[[outputs[tmp.format(iseg, jstep, kstage, var)]['value']
    #                            for kstage in range(1, num_stages + 1)]
    #                           for jstep in range(self.grid_data.num_steps_per_segment[iseg])]
    #                          for iseg in range(self.grid_data.num_segments)]]
    #         stage_units = outputs[tmp.format(0, 0, 1, var)]['units']
    #         stage_values = np.asarray(stage_values).ravel()
    #         output_value[gd.subset_node_indices['stage_nodes']] = convert_units(stage_values,
    #                                                                             stage_units,
    #                                                                             units)
    #
    #         # Populate the step end values
    #         step_end_idxs = gd.subset_node_indices['step_ends'][1::2]
    #         for iseg in range(gd.num_segments):
    #             for jstep in range(self.grid_data.num_steps_per_segment[iseg]):
    #                 advance_units = outputs[path +
    # 'segments.seg_{0}.step_{1}.advance.states:{2}_f'.format(iseg, jstep, var)]['units']
    #                 val = outputs[path +
    # 'segments.seg_{0}.step_{1}.advance.states:{2}_f'.format(iseg, jstep, var)]['value']
    #                 output_value[step_end_idxs[jstep]] = convert_units(val, advance_units, units)
    #
    #     elif var_type in ('input_control', 'indep_control'):
    #         var_path = path + path_map[var_type].format(var)
    #         output_units = outputs[var_path]['units']
    #
    #         vals = outputs[var_path]['value']
    #         output_value = convert_units(vals, output_units, units)
    #
    #     elif var_type in ('design_parameter', 'input_parameter', 'traj_design_parameter',
    #                       'traj_input_parameter'):
    #         var_path = path + path_map[var_type].format(var)
    #         output_units = outputs[var_path]['units']
    #
    #         output_value = convert_units(outputs[var_path]['value'], output_units, units)
    #         output_value = np.repeat(output_value, gd.num_nodes, axis=0)
    #
    #     elif var_type == 'ode':
    #         rhs_all_outputs = dict(self.rhs_all.list_outputs(out_stream=None, values=True,
    #                                                          shape=True, units=True))
    #         prom2abs_all = self.rhs_all._var_allprocs_prom2abs_list
    #         abs_path_all = prom2abs_all['output'][var][0]
    #         output_value = rhs_all_outputs[abs_path_all]['value']
    #         output_units = rhs_all_outputs[abs_path_all]['units']
    #         output_value = convert_units(output_value, output_units, units)
    #     else:
    #         var_path = path + path_map[var_type].format(var)
    #         output_units = outputs[var_path]['units']
    #         output_value = convert_units(outputs[var_path]['value'], output_units, units)
    #
    #     # Always return a column vector
    #     if len(output_value.shape) == 1:
    #         output_value = np.reshape(output_value, (gd.num_nodes, 1))
    #
    #     return output_value[gd.subset_node_indices[nodes], ...]
