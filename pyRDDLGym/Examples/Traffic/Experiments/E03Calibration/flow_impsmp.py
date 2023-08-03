import jax
import optax
import jax.numpy as jnp
import haiku as hk
import numpy as np
from tensorflow_probability.substrates import jax as tfp
import functools

from sys import argv
from time import perf_counter as timer
from datetime import datetime
import matplotlib.pyplot as plt
import seaborn as sns
import json

from pyRDDLGym import ExampleManager
from pyRDDLGym import RDDLEnv
from pyRDDLGym.Core.Jax.JaxRDDLLogic import FuzzyLogic
from pyRDDLGym.Core.Jax.JaxRDDLBackpropPlanner import JaxRDDLCompilerWithGrad

t0 = timer()

from jax.config import config as jconfig
use64bit = False # tfp.bijectors.SoftClip seems to have a bug with float64
jconfig.update('jax_debug_nans', True)
jconfig.update('jax_platform_name', 'cpu')
jconfig.update('jax_enable_x64', use64bit)
jnp.set_printoptions(
    linewidth=9999,
    formatter={'float': lambda x: "{0:0.3f}".format(x)})


key = jax.random.PRNGKey(3264)


# specify the model
EnvInfo = ExampleManager.GetEnvInfo('traffic4phase')
myEnv = RDDLEnv.RDDLEnv(domain=EnvInfo.get_domain(),
#                        instance='instances/instance01.rddl')
                        instance='instances/instance_2x2.rddl')

model = myEnv.model
N = myEnv.numConcurrentActions

init_state_subs, t_warmup = myEnv.sampler.subs, 0
t_plan = myEnv.horizon
rollout_horizon = t_plan - t_warmup

compiler = JaxRDDLCompilerWithGrad(
    rddl=model,
    use64bit=use64bit,
    logic=FuzzyLogic(weight=15))
compiler.compile()


# Define policy fn
# Every traffic light is assumed to follow a fixed-time plan
all_red_stretch = [1.0, 0.0, 0.0, 0.0]
protected_left_stretch = [1.0] + [0.0]*19
through_stretch = [1.0] + [0.0]*59
full_cycle = (all_red_stretch + protected_left_stretch + all_red_stretch + through_stretch)*2
BASE_PHASING = jnp.array(full_cycle*10, dtype=compiler.REAL)
FIXED_TIME_PLAN = jnp.broadcast_to(
    BASE_PHASING[..., jnp.newaxis],
    shape=(BASE_PHASING.shape[0], N))

def policy_fn(key, policy_params, hyperparams, step, states):
    return {'advance': FIXED_TIME_PLAN[step]}


# The difference in the batched and sequential rollout samplers
# is in the shapes of the argument and return arrays. Because
# jit-compilation requires static array shapes, the two sampler
# types are compiled separately

# number of rollouts per shard (e.g. for splitting computation on a GPU)
n_rollouts = 128
# number of shards per batch
n_shards = 8
batch_size = n_rollouts * n_shards

sampler = compiler.compile_rollouts(policy=policy_fn,
                                    n_steps=rollout_horizon,
                                    n_batch=n_rollouts)
sampler_hmc = compiler.compile_rollouts(policy=policy_fn,
                                        n_steps=rollout_horizon,
                                        n_batch=1)


# Set up initial states
subs_train = {}
subs_hmc = {}
for (name, value) in init_state_subs.items():
    value = jnp.array(value)[jnp.newaxis, ...]
    train_value = jnp.repeat(value, repeats=n_rollouts, axis=0)
    train_value = train_value.astype(compiler.REAL)
    subs_train[name] = train_value

    hmc_value = jnp.repeat(value, repeats=1, axis=0)
    hmc_value = hmc_value.astype(compiler.REAL)
    subs_hmc[name] = hmc_value
for (state, next_state) in model.next_state.items():
    subs_train[next_state] = subs_train[state]
    subs_hmc[next_state] = subs_hmc[state]


# Set up ground truth inflow rates
source_indices = range(16,24)
true_source_rates = jnp.array([0.3, 0.2, 0.1, 0.4, 0.1, 0.2, 0.3])
num_nonzero_sources = 7
source_rates = true_source_rates[:num_nonzero_sources]
s0, s1, s2 = source_indices[0], source_indices[num_nonzero_sources], source_indices[-1]

one_hot_inputs = jnp.eye(num_nonzero_sources)


# Prepare the ground truth inflow rates
base_rates = jnp.broadcast_to(
    source_rates[jnp.newaxis, ...],
    shape=(n_rollouts,s1-s0))

# Prepare the ground truth for batched rollouts
subs_train['SOURCE-ARRIVAL-RATE'] = subs_train['SOURCE-ARRIVAL-RATE'].at[:,s0:s1].set(base_rates)
subs_train['SOURCE-ARRIVAL-RATE'] = subs_train['SOURCE-ARRIVAL-RATE'].at[:,s1:s2].set(0.)
rollouts = sampler(
        key,
        policy_params=None,
        hyperparams=None,
        subs=subs_train,
        model_params=compiler.model_params)
GROUND_TRUTH_LOOPS = rollouts['pvar']['flow-into-link'][:,:,:]

# Prepare the ground truth for sequential rollouts
subs_hmc['SOURCE-ARRIVAL-RATE'] = subs_hmc['SOURCE-ARRIVAL-RATE'].at[:,s0:s1].set(source_rates)
subs_hmc['SOURCE-ARRIVAL-RATE'] = subs_hmc['SOURCE-ARRIVAL-RATE'].at[:,s1:s2].set(0.)
rollout_hmc = sampler_hmc(
        key,
        policy_params=None,
        hyperparams=None,
        subs=subs_hmc,
        model_params=compiler.model_params)
GROUND_TRUTH_LOOPS_HMC = rollout_hmc['pvar']['flow-into-link'][:,:,:]

softplus_obj = tfp.bijectors.SoftClip(low=0., high=0.4)
softplus_fwd = softplus_obj.forward
softplus_inv = softplus_obj.inverse
Jsoftplus_inv = jax.jacrev(softplus_inv)
density_correction = lambda a: jnp.abs(jnp.linalg.det(Jsoftplus_inv(a)))

def parametrized_policy(input):
    # Currently assumes a multivariate normal policy with
    # a diagonal covariance matrix

#        last_layer_bias_init = hk.initializers.Constant(jnp.array([-1.821, -1.82118], dtype=compiler.REAL))
    mlp = hk.Sequential([
        hk.Linear(32), jax.nn.relu,
        hk.Linear(32), jax.nn.relu,
#            hk.Linear(2, b_init=last_layer_bias_init)
        hk.Linear(2)
    ])
    output = jax.nn.softplus(mlp(input))
#        output = mlp(input)
    mean, cov = (jnp.squeeze(x) for x in jnp.split(output, 2, axis=1))
    return mean, jnp.diag(cov)

policy = hk.transform(parametrized_policy)
policy_apply = jax.jit(policy.apply)
key, subkey = jax.random.split(key)
theta = policy.init(subkey, one_hot_inputs)

@jax.jit
def policy_pdf(theta, rng, actions):
    mean, cov = policy_apply(theta, rng, one_hot_inputs)
    unconstrained_actions = softplus_inv(actions)
    normal_pdf = jax.scipy.stats.multivariate_normal.pdf(unconstrained_actions, mean=mean, cov=cov)
    correction = jnp.apply_along_axis(density_correction, axis=1, arr=actions)
    return normal_pdf * correction

@jax.jit
def policy_sample(theta, rng):
    mean, cov = policy_apply(theta, rng, one_hot_inputs)
    action_sample = jax.random.multivariate_normal(
        rng, mean, cov,
        shape=(n_rollouts,))
    action_sample = softplus_fwd(action_sample)
    return action_sample

@jax.jit
def reward_batched(rng, subs, actions):
    subs['SOURCE-ARRIVAL-RATE'] = subs['SOURCE-ARRIVAL-RATE'].at[:,s0:s1].set(actions)
    rollouts = sampler(
        rng,
        policy_params=None,
        hyperparams=None,
        subs=subs,
        model_params=compiler.model_params)
    delta = rollouts['pvar']['flow-into-link'][:,:,:] - GROUND_TRUTH_LOOPS
    return jnp.sum(delta*delta, axis=(1,2))

@jax.jit
def reward_seqtl(rng, subs, action):
    subs['SOURCE-ARRIVAL-RATE'] = subs['SOURCE-ARRIVAL-RATE'].at[:,s0:s1].set(action)
    rollout = sampler_hmc(
        rng,
        policy_params=None,
        hyperparams=None,
        subs=subs,
        model_params=compiler.model_params)
    delta = rollout['pvar']['flow-into-link'][:,:,:] - GROUND_TRUTH_LOOPS_HMC
    return jnp.sum(delta*delta, axis=(1,2))

clip_val = jnp.array([1e-6], dtype=compiler.REAL)

def scalar_mult(alpha, v):
    return alpha * v

weighting_map = jax.jit(jax.vmap(
    scalar_mult, in_axes=0, out_axes=0))

@jax.jit
def collect_covar_data(theta, C, iter):
    ip0 = 0
    for leaf in jax.tree_util.tree_leaves(theta):
        leaf = leaf.flatten()
        ip1 = ip0 + leaf.shape[0]
        C = C.at[ip0:ip1, iter].set(leaf)
        ip0 = ip1
    return C


def reinforce(key, n_iters, theta, subs_train, subs_hmc=None, est_Z=None):
    # (The arguments that default to 'None' are included to have a consistent signature
    #  with the impsmp function)

    # initialize stats collection
    X, Y = np.arange(n_iters), np.zeros(shape=(n_iters,5,num_nonzero_sources))
    n_params = sum(leaf.flatten().shape[0] for leaf in jax.tree_util.tree_leaves(theta))
    C = jnp.zeros(shape=(n_params, n_iters))

    # initialize optimizer
    #optimizer = optax.sgd(learning_rate=1e-5)
    optimizer = optax.rmsprop(learning_rate=1e-3, momentum=0.1)
    opt_state = optimizer.init(theta)

    # run
    for idx in range(n_iters):
        subt0 = timer()

        key, subkey = jax.random.split(key)
        mean, cov = policy_apply(theta, subkey, one_hot_inputs)

        grads = jax.tree_util.tree_map(lambda _: jnp.zeros(1), theta)
        cmlt_rewards = 0
        cmlt_actions = np.empty(shape=(batch_size, num_nonzero_sources))
        for si in range(n_shards):
            key, *subkeys = jax.random.split(key, num=3)
            actions = policy_sample(theta, subkeys[0])
            pi = policy_pdf(theta, subkeys[0], actions)
            dpi = jax.jacrev(policy_pdf, argnums=0)(theta, subkeys[0], actions)
            rewards = reward_batched(subkeys[0], subs_train, actions)
            factors = rewards / (pi + 1e-6)

            w = lambda x: jnp.mean(weighting_map(factors, x), axis=0)

            accumulant = jax.tree_util.tree_map(w, dpi)
            grads = jax.tree_util.tree_map(lambda x, y: x+(y/n_shards), grads, accumulant)
            cmlt_rewards += jnp.mean(rewards)/n_shards
            cmlt_actions[si*n_rollouts:(si+1)*n_rollouts] = actions

        updates, opt_state = optimizer.update(grads, opt_state)
        theta = optax.apply_updates(theta, updates)

        Y[idx,0] = cmlt_rewards
        Y[idx,1] = mean
        Y[idx,2] = jnp.diag(cov)
        Y[idx,3] = np.mean(cmlt_actions, axis=0)
        Y[idx,4] = np.std(cmlt_actions, axis=0)
        subt1 = timer()
        print(f'Iter {idx} :: REINFORCE :: Runtime={subt1-subt0}s')
        print(f'Untransformed parametrized policy [Mean, Diag(Cov)] = \n{Y[idx,1:3]}')
        print(f'Transformed action sample means, stdevs = \n{Y[idx,3:5]}')
        print(f'Eval. reward={Y[idx,0,0]}\n')

        C = collect_covar_data(theta, C, idx)

    return X, Y, C


# === REINFORCE with Importance Sampling ====
impsmp_config = {
    'num_iters': int(batch_size),
    'step_size': 0.1,
    'optimizer': 'rmsprop',
    'lr': 1e-3,
    'momentum': 0.1,
    'est_Z': False,
}
impsmp_config['num_burnin_steps'] = int(impsmp_config['num_iters']/8)


@jax.jit
def unnormalized_log_rho(key, theta, subs, a):
    #dpi = jax.jacrev(policy_pdf, argnums=0)(theta, key, a)
    dpi = jax.grad(policy_pdf, argnums=0)(theta, key, a)
    #dpi_norm = jax.tree_util.tree_reduce(lambda x,y: x + jnp.sum((y/100)**2), dpi, initializer=jnp.zeros(1))
    #dpi_norm = 100*jnp.sqrt(dpi_norm)
    dpi_norm = jax.tree_util.tree_reduce(lambda x, y: jnp.maximum(x, jnp.max(jnp.abs(y))), dpi, initializer=jnp.array([-jnp.inf]))
    rewards = reward_seqtl(key, subs, a)
    density = jnp.abs(rewards) * dpi_norm
    return jnp.log(jnp.maximum(density, clip_val))[0]

batch_unnormalized_log_rho = jax.jit(jax.vmap(
    unnormalized_log_rho, (None, None, None, 0), 0))


def impsmp(key, n_iters, theta, subs_train, subs_hmc, est_Z=False):

    # initialize stats collection
    X, Y = np.arange(n_iters), np.zeros(shape=(n_iters, 3, num_nonzero_sources))
    n_params = sum(leaf.flatten().shape[0] for leaf in jax.tree_util.tree_leaves(theta))
    C = jnp.zeros(shape=(n_params, n_iters))

    # initialize hmc
    key, subkey = jax.random.split(key, num=2)
    hmc_initializer = jax.random.uniform(
        subkey,
        shape=(num_nonzero_sources,),
        minval=0.05, maxval=0.35)

    # initialize optimizer
    if impsmp_config['optimizer'] == 'rmsprop':
        optimizer = optax.rmsprop(
            learning_rate=impsmp_config['lr'], momentum=impsmp_config['momentum'])
    elif impsmp_config['optimizer'] == 'adam':
        optimizer = optax.adam(learning_rate=impsmp_config['lr'])
    elif impsmp_config['optimizer'] == 'sgd':
        optimizer = optax.sgd(
            learning_rate=impsmp_config['lr'], momentum=impsmp_config['momentum'])
    else: raise ValueError
    opt_state = optimizer.init(theta)

    # initialize unconstraining bijector
    unconstraining_bijector = [
        #tfp.bijectors.SoftClip(low=0., high=0.4)
        tfp.bijectors.Softplus()
        #tfp.bijectors.Identity()
    ]

    # run
    for idx in range(n_iters):
        subt0 = timer()
        key, *subkeys = jax.random.split(key, num=6)

        log_density = functools.partial(
            unnormalized_log_rho, subkeys[0], theta, subs_hmc)

        adaptive_hmc_kernel = tfp.mcmc.TransformedTransitionKernel(
            inner_kernel = tfp.mcmc.SimpleStepSizeAdaptation(
                inner_kernel=tfp.mcmc.HamiltonianMonteCarlo(
                    target_log_prob_fn=log_density,
                    num_leapfrog_steps=3,
                    step_size=impsmp_config['step_size']),
                num_adaptation_steps=int(impsmp_config['num_burnin_steps'] * 0.8)),
            bijector=unconstraining_bijector)

        samples, is_accepted = tfp.mcmc.sample_chain(
            seed=subkeys[1],
            num_results=impsmp_config['num_iters'],
            num_burnin_steps=impsmp_config['num_burnin_steps'],
            current_state=hmc_initializer,
            kernel=adaptive_hmc_kernel,
            trace_fn=lambda _, pkr: pkr.inner_results.inner_results.is_accepted)

        print(samples[:10])
        print(jnp.mean(samples, axis=0))

        grads = jax.tree_util.tree_map(lambda _: jnp.zeros(1), theta)
        cmlt_rewards = 0.
        for si in range(n_shards):
            actions = samples[si*n_rollouts:(si+1)*n_rollouts]
            dpi = jax.jacrev(policy_pdf, argnums=0)(theta, subkeys[2], actions)
            rewards = reward_batched(subkeys[2], subs_train, actions)
            rho = jnp.exp(batch_unnormalized_log_rho(subkeys[3], theta, subs_hmc, actions))
            factors = rewards / (rho + 1e-8)

            if est_Z:
                # fix this to accumulate Z instead of recomputing every si
                pi = policy_pdf(theta, subkeys[2], actions)
                Zinv = jnp.sum(pi/rho)
                Zinv = jnp.maximum(Zinv, clip_val)
                factors = factors / Zinv

            w = lambda x: jnp.mean(weighting_map(factors, x), axis=0)

            accumulant = jax.tree_util.tree_map(w, dpi)
            grads = jax.tree_util.tree_map(lambda x, y: x+(y/n_shards), grads, accumulant)
            cmlt_rewards += jnp.mean(rewards)/n_shards

        updates, opt_state = optimizer.update(grads, opt_state)
        theta = optax.apply_updates(theta, updates)

        # Initialize the next chain at a random point of the current chain
        hmc_intializer = jax.random.choice(subkeys[4], samples)

        mean, cov = policy_apply(theta, subkeys[0], one_hot_inputs)
        eval_actions = policy_sample(theta, subkeys[0])
        eval_rewards = jnp.mean(reward_batched(subkeys[0], subs_train, eval_actions))

        Y[idx,0,0] = eval_rewards
        Y[idx,0,1] = cmlt_rewards
        Y[idx,1] = mean
        Y[idx,2] = jnp.diag(cov)
        subt1 = timer()
        print(f'Iter {idx} :: Importance Sampling :: Runtime={subt1-subt0}s :: HMC acceptance rate={jnp.mean(is_accepted)*100:.2f}%')
        print(f'[Mean, Diag(Cov)] = \n{Y[idx,1:]}')
        print(f'HMC sample reward={Y[idx,0,1]} :: Eval reward={Y[idx,0,0]}\n')

        C = collect_covar_data(theta, C, idx)

    return X, Y, C

# === Debugging utils ===
def print_shapes(x):
    print(x.shape)

def eval_single_rollout(a):
    a = jnp.asarray(a)
    rewards = reward_seqtl(key, subs_hmc, a)
    return rewards

def rndsmp(key, n_iters, theta, subs_train, subs_hmc=None, est_Z=None):
    # Sanity check benchmark. How well does randomly sampling the actions perform?
    # We often observe that the means learn to become small and variances large.

    # initialize stats collection
    X, Y = np.arange(n_iters), np.zeros(shape=(n_iters,3,num_nonzero_sources))
    n_params = sum(leaf.flatten().shape[0] for leaf in jax.tree_util.tree_leaves(theta))
    C = jnp.zeros(shape=(n_params, n_iters))

    # initialize optimizer
    #optimizer = optax.sgd(learning_rate=1e-5)
    optimizer = optax.rmsprop(learning_rate=1e-3)
    opt_state = optimizer.init(theta)

    # run
    for idx in range(n_iters):
        subt0 = timer()

        grads = jax.tree_util.tree_map(lambda _: jnp.zeros(1), theta)
        cmlt_rewards = 0
        for _ in range(n_shards):
            key, *subkeys = jax.random.split(key, num=3)
            actions = jax.random.uniform(
                subkeys[0],
                shape=(n_rollouts, num_nonzero_sources),
                minval=0., maxval=0.4)
            pi = policy_pdf(theta, subkeys[1], actions)
            dpi = jax.jacrev(policy_pdf, argnums=0)(theta, subkeys[1], actions)
            rewards = reward_batched(subkeys[1], subs_train, actions)
            factors = rewards / (pi + 1e-6)

            w = lambda x: jnp.mean(weighting_map(factors, x), axis=0)

            accumulant = jax.tree_util.tree_map(w, dpi)
            grads = jax.tree_util.tree_map(lambda x, y: x+(y/n_shards), grads, accumulant)
            cmlt_rewards += jnp.mean(rewards)/n_shards

        updates, opt_state = optimizer.update(grads, opt_state)
        theta = optax.apply_updates(theta, updates)

        key, subkey = jax.random.split(key)
        mean, cov = policy_apply(theta, subkey, one_hot_inputs)
        eval_actions = policy_sample(theta, subkey)
        eval_rewards = jnp.mean(reward_batched(subkey, subs_train, eval_actions))

        Y[idx,0,0] = eval_rewards
        Y[idx,0,1] = cmlt_rewards
        Y[idx,1] = mean
        Y[idx,2] = jnp.diag(cov)
        subt1 = timer()
        print(f'Iter {idx} :: Random Sampling :: Runtime={subt1-subt0}s')
        print(f'[Mean, Diag(Cov)] = \n{Y[idx,1:]}')
        print(f'Random sample reward={Y[idx,0,1]} :: Eval. reward={Y[idx,0,0]}\n')

        C = collect_covar_data(theta, C, idx)

    return X, Y, C



method = 'reinforce'
if method == 'impsmp': method_fn = impsmp
elif method == 'reinforce': method_fn = reinforce
elif method == 'rndsmp': method_fn = rndsmp
else: raise KeyError


id = f'{method}_2x2_b{batch_size}_nS{num_nonzero_sources}'
timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')

with jax.disable_jit(disable=False):
    key, subkey = jax.random.split(key)
    t1 = timer()
    X, Y, C = method_fn(key=subkey, n_iters=250, theta=theta, subs_train=subs_train, subs_hmc=subs_hmc, est_Z=impsmp_config['est_Z'])
    t2 = timer()

thetacov = jnp.cov(C)
sns.heatmap(thetacov)
plt.savefig(f'img/{timestamp}_{id}_covar.png')

with open(f'img/{timestamp}_{id}.json', 'w') as file:
    savedata = {
        'num_nonzero_sources': num_nonzero_sources,
        'X': X.tolist(),
        'Y': Y.T.tolist(),}
        #'C': C.tolist(),
        #'thetacov': thetacov.tolist()}
    if method == 'impsmp':
        savedata.update(impsmp_config)
    json.dump(savedata, file)

print('Setup took', t1-t0, 'seconds')
print('Optimization took', t2-t1, 'seconds')
