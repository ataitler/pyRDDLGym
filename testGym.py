from Env import RDDLEnv as RDDLEnv
from Policies.Agents import RandomAgent
# from Visualizer.MarsRoverViz import MarsRoverVisualizer
from Visualizer.PowerGenViz import PowerGenVisualizer

# PROBLEM = 'RDDL/Thiagos_HVAC_grounded.rddl'
# PROBLEM = 'RDDL/Thiagos_Mars_Rover.rddl'
# PROBLEM = 'RDDL/Thiagos_HVAC.rddl'
# FOLDER = 'Competition/Power_gen/'
# FOLDER = 'Competition/Mars_rover/'
# FOLDER = 'Competition/UAVs_con/'
# FOLDER = 'Competition/Wildfire/'
# FOLDER = 'Competition/MountainCar2/'
# FOLDER = 'Competition/Cartpole2/'
# FOLDER = 'Competition/Elevator/'
# FOLDER = 'Competition/Recsim/'
# FOLDER = 'Competition/Pendulum/'
FOLDER = 'Competition/test/'



def main():
    myEnv = RDDLEnv.RDDLEnv(domain=FOLDER+'domain.rddl', instance=FOLDER+'instance0.rddl', is_grounded=False)
    agent = RandomAgent(action_space=myEnv.action_space, num_actions=myEnv.NumConcurrentActions)
    myEnv.set_visualizer(PowerGenVisualizer)

    total_reward = 0
    state = myEnv.reset()
    for step in range(myEnv.horizon):
        myEnv.render()
        action = agent.sample_action()
        next_state, reward, done, info = myEnv.step(action)
        total_reward += reward
        print()
        print('step       = {}'.format(step))
        print('state      = {}'.format(state))
        print('action     = {}'.format(action))
        print('next state = {}'.format(next_state))
        print('reward     = {}'.format(reward))
        state = next_state
        if done:
            break
    print("episode ended with reward {}".format(total_reward))





if __name__ == "__main__":
    main()