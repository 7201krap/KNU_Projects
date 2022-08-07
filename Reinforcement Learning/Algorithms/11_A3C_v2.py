import gym
import numpy as np
import torch as T
import torch.multiprocessing as mp
import torch.nn as nn
import torch.nn.functional as F
import matplotlib.pyplot as plt
from torch.distributions import Categorical

# define global variable here
# If you are running two separate processes, then they won't be sharing the same globals.
# If you want to pass the data between the processes, look at using send and recv.
# https://stackoverflow.com/questions/11215554/globals-variables-and-python-multiprocessing
N_GAMES = 4000
T_MAX = 5
rewards = []


# shared Adam optimizer
# all worker shares the same Adam optimizer
class SharedAdam(T.optim.Adam):
    def __init__(self, params, lr=1e-3, betas=(0.9, 0.99), eps=1e-8, weight_decay=0):
        super(SharedAdam, self).__init__(params, lr=lr, betas=betas, eps=eps, weight_decay=weight_decay)

        for group in self.param_groups:
            for p in group['params']:
                state = self.state[p]
                state['step'] = 0
                state['exp_avg'] = T.zeros_like(p.data)
                state['exp_avg_sq'] = T.zeros_like(p.data)

                state['exp_avg'].share_memory_()
                state['exp_avg_sq'].share_memory_()


# actor and critic class
class ActorCritic(nn.Module):
    def __init__(self, input_dims, n_actions, gamma=0.99):
        super(ActorCritic, self).__init__()

        self.gamma = gamma

        # policy network
        self.policy_l1 = nn.Linear(*input_dims, 128)
        self.policy_l2 = nn.Linear(128, n_actions)

        # value network
        self.value_l1 = nn.Linear(*input_dims, 128)
        self.value_l2 = nn.Linear(128, 1)

        # use these as accumulators
        self.rewards = []
        self.actions = []
        self.states = []
        self.next_states = []

    # accumulate state, action, reward for a period of time
    def remember(self, state, action, reward, next_state):
        self.states.append(state)
        self.actions.append(action)
        self.rewards.append(reward)
        self.next_states.append(next_state)

    # once the episode terminates OR agent moves T_MAX steps clear the batch(=memory)
    def clear_memory(self):
        self.states = []
        self.actions = []
        self.rewards = []
        self.next_states = []

    # forward state to the policy and value network
    def forward(self, state):
        p1 = F.relu(self.policy_l1(state))
        p2 = self.policy_l2(p1)  # final policy output. There are two outputs for the policy network

        v1 = F.relu(self.value_l1(state))
        v2 = self.value_l2(v1)  # final value output. There is one output for the value network

        return p2, v2  # We will after softmax p2 so that the action could be represented as a probability (L or R)

    # calculate return (= TD target)
    def calc_R(self, done):
        states = T.tensor(self.states, dtype=T.float)
        next_states = T.tensor(self.next_states, dtype=T.float)

        # forward all collected states
        _, v = self.forward(states)
        _, v_next = self.forward(next_states)

        # print("states shape", states.size())
        # print("v shape", v.size())
        # print("rewards shape", len(self.rewards))

        R = v_next[-1] * (1 - int(done))

        batch_return = []

        for reward in self.rewards[::-1]:
            R = reward + self.gamma * R
            batch_return.append(R)
        batch_return.reverse()
        batch_return = T.tensor(batch_return, dtype=T.float)

        return batch_return

    def calc_loss(self, done):
        states = T.tensor(self.states, dtype=T.float)
        actions = T.tensor(self.actions, dtype=T.float)

        returns = self.calc_R(done)  # returns (= TD-targets)

        pi, values = self.forward(states)
        values = values.squeeze()
        critic_loss = (returns - values) ** 2

        probs = T.softmax(pi, dim=1)
        dist = Categorical(probs)
        log_probs = dist.log_prob(actions)
        actor_loss = -log_probs * (returns - values)

        total_loss = (critic_loss + actor_loss).mean()

        return total_loss

    def choose_action(self, state):
        state = T.tensor([state], dtype=T.float)
        pi, v = self.forward(state)
        probs = T.softmax(pi, dim=1)
        dist = Categorical(probs)
        action = dist.sample().numpy()[0]

        return action


class Agent(mp.Process):
    def __init__(self, global_actor_critic, optimizer, input_dims, n_actions,
                 gamma, lr, name, global_ep_idx, env_id):
        super(Agent, self).__init__()
        self.local_actor_critic = ActorCritic(input_dims, n_actions, gamma)
        self.global_actor_critic = global_actor_critic
        self.name = 'w%02i' % name
        self.episode_idx = global_ep_idx
        self.env = gym.make(env_id)
        self.optimizer = optimizer  # global network
        self.max_episode = 0

    def run(self):
        global rewards
        t_step = 1

        while self.episode_idx.value < N_GAMES:
            done = False
            state = self.env.reset()
            score = 0
            self.local_actor_critic.clear_memory()

            while not done:
                action = self.local_actor_critic.choose_action(state)
                next_state, reward, done, info = self.env.step(action)
                score += reward

                # accumulate state, action, reward pairs to the batch
                self.local_actor_critic.remember(state, action, reward, next_state)

                if t_step % T_MAX == 0 or done:
                    loss = self.local_actor_critic.calc_loss(done)
                    self.optimizer.zero_grad()
                    loss.backward()

                    # local network passes parameter to the global network
                    for local_param, global_param in zip(self.local_actor_critic.parameters(),
                                                         self.global_actor_critic.parameters()):
                        global_param._grad = local_param.grad

                    # global network's step
                    self.optimizer.step()

                    # copy the global network to the local network
                    self.local_actor_critic.load_state_dict(self.global_actor_critic.state_dict())

                    # clear the batch(=memory)
                    self.local_actor_critic.clear_memory()

                t_step += 1
                state = next_state

            with self.episode_idx.get_lock():
                self.episode_idx.value += 1

            print('worker: ', self.name, 'episode: ', self.episode_idx.value, 'reward: ', score)
            rewards.append(score)

        # plot rewards for every worker here
        fig, ax = plt.subplots(figsize=(10, 6))
        ax.plot(np.linspace(0, len(rewards), len(rewards)), rewards)
        ax.set_title(f'Worker {self.name}')
        ax.set_xlabel('Episode')
        ax.set_ylabel('Reward')
        plt.show()


if __name__ == '__main__':
    print("number of cores:", mp.cpu_count())
    lr = 1e-4
    env_id = 'CartPole-v1'
    n_actions = 2
    input_dims = [4]

    global_actor_critic = ActorCritic(input_dims, n_actions)
    global_actor_critic.share_memory()
    optim = SharedAdam(global_actor_critic.parameters(),
                       lr=lr,
                       betas=(0.92, 0.999))
    global_ep = mp.Value('i', 0)

    workers = [Agent(global_actor_critic,
                     optim,
                     input_dims,
                     n_actions,
                     gamma=0.99,
                     lr=lr,
                     name=i,
                     global_ep_idx=global_ep,
                     env_id=env_id
                     ) for i in range(mp.cpu_count())
               ]

    [w.start() for w in workers]
    [w.join() for w in workers]

    # see the following for the join() method:
    # https://stackoverflow.com/questions/25391025/what-exactly-is-python-multiprocessing-modules-join-method-doing
