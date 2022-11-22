import jax
import numpy as np
from typing import Dict
import warnings

from pyRDDLGym.Core.ErrorHandling.RDDLException import RDDLActionPreconditionNotSatisfiedError
from pyRDDLGym.Core.ErrorHandling.RDDLException import RDDLInvalidExpressionError
from pyRDDLGym.Core.ErrorHandling.RDDLException import RDDLStateInvariantNotSatisfiedError
from pyRDDLGym.Core.Jax.JaxRDDLCompiler import JaxRDDLCompiler
from pyRDDLGym.Core.Parser.expr import Value
from pyRDDLGym.Core.Simulator.RDDLSimulator import RDDLSimulator

Args = Dict[str, Value]


class JaxRDDLSimulator(RDDLSimulator):
    
    DEFAULT_VALUES = {
        'int': 0,
        'real': 0.0,
        'bool': False
    }
    
    def __init__(self, 
                 compiled_rddl: JaxRDDLCompiler,
                 key: jax.random.PRNGKey,
                 soft_error: bool=True) -> None:
        self.rddl = compiled_rddl.rddl
        self.key = key
        self.soft = soft_error
        
        # compile Jax program
        data = compiled_rddl.compile()        
        self.invariants = data['invariants']
        self.preconds = data['preconds']
        self.terminals = data['terminals']
        _, self.reward = data['reward']
        _, self.cpfs = data['cpfs']
        
        # retrieve CPF information
        self.state_vars = compiled_rddl.states.values()
        self.state_unprimed = {cpf: compiled_rddl.states.get(cpf, cpf) 
                               for cpf in self.cpfs.keys()}
        
        cpf_order = self.rddl.domain.derived_cpfs + \
                    self.rddl.domain.intermediate_cpfs + \
                    self.rddl.domain.state_cpfs + \
                    self.rddl.domain.observation_cpfs 
        self.cpfs = {cpf.pvar[1][0]: self.cpfs[cpf.pvar[1][0]] for cpf in cpf_order}

        # initialization of scalar and pvariables tensors
        object_lookup = {}        
        for obj, values in self.rddl.non_fluents.objects:
            object_lookup.update(zip(values, [obj] * len(values)))  
              
        self.init_values = {}
        for pvar in self.rddl.domain.pvariables:
            name = pvar.name
            params = pvar.param_types
            ptype = pvar.range
            pvalue = pvar.default
            
            # initial value from default statement if possible
            value = pvalue
            if value is None:
                value = JaxRDDLSimulator.DEFAULT_VALUES[ptype]
            if not compiled_rddl.allow_discrete:
                value = float(value)
                ptype = 'real'
            
            # initialize value array of shape given by number of free parameters
            if params is None:
                self.init_values[name] = value              
            else: 
                self.init_values[name] = np.full(
                    shape=tuple(len(compiled_rddl.objects[p]) for p in params),
                    fill_value=value,
                    dtype=JaxRDDLCompiler.RDDL_TO_JAX_TYPE[ptype])
        
        # initialization of state
        if hasattr(self.rddl.instance, 'init_state'):
            for (name, params), value in self.rddl.instance.init_state:
                if params is not None:
                    coords = tuple(compiled_rddl.objects[object_lookup[p]][p] for p in params)
                    self.init_values[name][coords] = value   
        
        # initialization of non-fluents
        if hasattr(self.rddl.non_fluents, 'init_non_fluent'):
            for (name, params), value in self.rddl.non_fluents.init_non_fluent:
                if params is not None:
                    coords = tuple(compiled_rddl.objects[object_lookup[p]][p] for p in params)
                    self.init_values[name][coords] = value   
                    
        self.subs = self.init_values.copy()
        
    @staticmethod
    def _print_stack_trace(expr, subs, key):
        return str(jax.make_jaxpr(expr)(subs, key))
    
    def handle_error_code(self, error, aux_str):
        errors = [JaxRDDLCompiler.INVERSE_ERROR_CODES[i]
                  for i in JaxRDDLCompiler.get_error_codes(error)]
        if errors:
            error_list = '\n'.join('{}. {}'.format(i + 1, s) for i, s in enumerate(errors))
            error_message = 'Internal error(s) returned ' + \
                            'during evaluation of {}:\n'.format(aux_str) + \
                            error_list
            if self.soft:
                warnings.warn(error_message, FutureWarning, stacklevel=2)
            else:
                raise RDDLInvalidExpressionError(error_message)
    
    def _check_state_invariants(self) -> None:
        '''Throws an exception if the state invariants are not satisfied.'''
        for idx, invariant in enumerate(self.invariants):
            sample, self.key, error = invariant(self.subs, self.key)
            self.handle_error_code(error, 'invariant {}'.format(idx + 1))
            if not bool(sample):
                raise RDDLStateInvariantNotSatisfiedError(
                    'State invariant {} is not satisfied.'.format(idx + 1) + 
                    '\n' + JaxRDDLSimulator._print_stack_trace(invariant, self.subs, self.key))
    
    def check_action_preconditions(self) -> None:
        '''Throws an exception if the action preconditions are not satisfied.'''
        for idx, precond in enumerate(self.preconds):
            sample, self.key, error = precond(self.subs, self.key)
            self.handle_error_code(error, 'precondition {}'.format(idx + 1))
            if not bool(sample):
                raise RDDLActionPreconditionNotSatisfiedError(
                    'Action precondition {} is not satisfied.'.format(idx + 1) + 
                    '\n' + JaxRDDLSimulator._print_stack_trace(precond, self.subs, self.key))
    
    def check_terminal_states(self) -> bool:
        '''return True if a terminal state has been reached.'''
        for idx, terminal in enumerate(self.terminals):
            sample, self.key, error = terminal(self.subs, self.key)
            self.handle_error_code(error, 'termination {}'.format(idx + 1))
            if bool(sample):
                return True
        return False
    
    def sample_reward(self) -> float:
        '''Samples the current reward given the current state and action.'''
        reward, self.key, error = self.reward(self.subs, self.key)
        self.handle_error_code(error, 'reward function')
        return float(reward)
    
    def reset(self) -> Args:
        '''Resets the state variables to their initial values.'''
        self.subs = self.init_values.copy()  
        obs = {var: self.subs[var] for var in self.state_vars}
        done = self.check_terminal_states()
        return obs, done
    
    def step(self, actions: Args) -> Args:
        '''Samples and returns the next state from the cpfs.
        
        :param actions: a dict mapping current action fluents to their values
        '''
        subs, key = self.subs, self.key
        subs.update(actions)
        
        for name, cpf in self.cpfs.items():
            subs[name], key, error = cpf(subs, key)
            self.handle_error_code(error, 'CPF <{}>'.format(name))
        reward = self.sample_reward()
        for primed, unprimed in self.state_unprimed.items():
            subs[unprimed] = subs[primed]
            
        obs = {var: subs[var] for var in self.state_vars}
        self.key = key
        done = self.check_terminal_states()
        return obs, reward, done
        
