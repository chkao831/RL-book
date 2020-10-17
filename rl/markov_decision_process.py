from abc import ABC, abstractmethod
from collections import defaultdict
import dataclasses
from dataclasses import dataclass
import functools
import itertools
from typing import (DefaultDict, Dict, Iterable, Generic, Mapping,
                    Tuple, Sequence, TypeVar, Optional)

from rl.distribution import (Constant, Categorical, Choose, Distribution,
                             FiniteDistribution, SampledDistribution)
from rl.markov_process import (
    FiniteMarkovRewardProcess, MarkovRewardProcess, StateReward)

A = TypeVar('A')
S = TypeVar('S')


class Policy(ABC, Generic[S, A]):
    '''A policy is a function that specifies what we should do (the
    action) at a given state of our MDP.

    '''
    @abstractmethod
    def act(self, state: S) -> Optional[Distribution[A]]:
        pass


class Always(Policy[S, A]):
    action: A

    def __init__(self, action: A):
        self.action = action

    def act(self, _: S) -> Optional[Distribution[A]]:
        return Constant(self.action)


class FinitePolicy(Policy[S, A]):
    ''' A policy where the state and action spaces are finite.

    '''
    policy_map: Mapping[S, Optional[FiniteDistribution[A]]]

    def __init__(
        self,
        policy_map: Mapping[S, Optional[FiniteDistribution[A]]]
    ):
        self.policy_map = policy_map

    def __repr__(self) -> str:
        display = ""
        for s, d in self.policy_map.items():
            if d is None:
                display += f"{s} is a Terminal State\n"
            else:
                display += f"For State {s}:\n"
                for a, p in d:
                    display += f"  Do Action {a} with Probability {p:.3f}\n"
        return display

    def act(self, state: S) -> Optional[FiniteDistribution[A]]:
        return self.policy_map[state]

    def states(self) -> Iterable[S]:
        return self.policy_map.keys()


@dataclass
class Transition(Generic[S, A]):
    state: S
    action: A
    next_state: S
    reward: float


def returns(
        trace: Iterable[Transition[S, A]],
        γ: float = 1,
        n_states: int = 1
) -> Iterable[Transition[S, A]]:
    '''Given an iterator of transitions, calculate the return for each
    subsequent transition.

    Arguments:
    rewards -- transitions with instantaneous rewards
    γ -- the discount factor (0 < γ ≤ 1), default: 1
    n_states -- how many states to calculate the return for, default: 1

    Returns transitions with returns instead of instantaneous rewards.

    '''
    trace = list(itertools.islice(iter(trace), n_states))
    *transitions, last = trace

    def accum(r_acc, r):
        return r_acc + γ * r
    final_return = functools.reduce(accum, (t.reward for t in trace), 0.0)

    def update(
            acc: Transition[S, A],
            transition: Transition[S, A]
    ) -> Transition[S, A]:
        return dataclasses.replace(transition, reward=acc.reward)

    initial =\
        dataclasses.replace(last, reward=last.reward + γ * final_return)
    return itertools.accumulate(
        reversed(transitions), update, initial=initial
    )


class MarkovDecisionProcess(ABC, Generic[S, A]):
    @abstractmethod
    def apply_policy(self, policy: Policy[S, A]) -> MarkovRewardProcess[S]:
        pass

    @abstractmethod
    def actions(self, state: S) -> Iterable[A]:
        pass

    def step(
        self,
        state: S,
        action: A
    ) -> Optional[Distribution[Tuple[S, float]]]:
        return self.apply_policy(Always(action)).transition_reward(state)

    def simulate_actions(
            self,
            start_states: Distribution[S],
            policy: Policy[S, A]
    ) -> Iterable[Transition[S, A]]:
        '''Simulate this MDP with the given policy, yielding the actions taken
        at each step.

        '''
        state: S = start_states.sample()
        reward: float = 0

        while True:
            action_distribution = policy.act(state)
            if action_distribution is None:
                return

            action = action_distribution.sample()
            next_distribution = self.step(state, action)
            if next_distribution is None:
                return

            next_state, reward = next_distribution.sample()
            yield Transition(state, action, next_state, reward)
            state = next_state

    def action_traces(
            self,
            start_states: Distribution[S],
            policy: Policy[S, A]
    ) -> Iterable[Iterable[Transition[S, A]]]:
        '''Yield an infinite number of traces as returned by
        simulate_actions.

        '''
        while True:
            yield self.simulate_actions(start_states, policy)


ActionMapping = Mapping[A, StateReward[S]]
StateActionMapping = Mapping[S, Optional[ActionMapping[A, S]]]


class FiniteMarkovDecisionProcess(MarkovDecisionProcess[S, A]):
    '''A Markov Decision Process with finite state and action spaces.

    '''

    mapping: StateActionMapping[S, A]
    non_terminal_states: Sequence[S]

    def __init__(self, mapping: StateActionMapping[S, A]):
        self.mapping = mapping
        self.non_terminal_states = [s for s, v in mapping.items()
                                    if v is not None]

    def __repr__(self) -> str:
        display = ""
        for s, d in self.mapping.items():
            if d is None:
                display += f"{s} is a Terminal State\n"
            else:
                display += f"From State {s}:\n"
                for a, d1 in d.items():
                    display += f"  With Action {a}:\n"
                    for (s1, r), p in d1:
                        display += f"    To [State {s1} and "\
                            + f"Reward {r:.3f}] with Probability {p:.3f}\n"
        return display

    # Note: We need both apply_policy and apply_finite_policy because,
    # to be compatible with MarkovRewardProcess, apply_policy has to
    # work even if the policy is *not* finite.
    def apply_policy(self, policy: Policy[S, A]) -> MarkovRewardProcess[S]:
        mapping = self.mapping

        class Process(MarkovRewardProcess[S]):

            def transition_reward(self, state: S)\
                    -> Optional[SampledDistribution[Tuple[S, float]]]:

                action_map: Optional[ActionMapping[A, S]] = mapping[state]

                if action_map is None:
                    return None

                def next_pair(action_map=action_map):
                    action: A = policy.act(state).sample()
                    return action_map[action].sample()

                return SampledDistribution(next_pair)

            def sample_states(self) -> Distribution[S]:
                return Choose(set(mapping.keys()))

        return Process()

    def apply_finite_policy(self, policy: FinitePolicy[S, A])\
            -> FiniteMarkovRewardProcess[S]:

        transition_mapping: Dict[S, Optional[StateReward[S]]] = {}

        for state in self.mapping:
            action_map: Optional[ActionMapping[A, S]] = self.mapping[state]

            if action_map is None:
                transition_mapping[state] = None
            else:
                outcomes: DefaultDict[Tuple[S, float], float]\
                    = defaultdict(float)

                actions = policy.act(state)
                if actions is not None:
                    for action, p_action in actions:
                        for outcome, p_state_reward in action_map[action]:
                            outcomes[outcome] += p_action * p_state_reward

                transition_mapping[state] = Categorical(outcomes)

        return FiniteMarkovRewardProcess(transition_mapping)

    def action_mapping(self, state: S) -> Optional[ActionMapping[A, S]]:
        return self.mapping[state]

    # Note: For now, this is only available on finite MDPs; this might
    # change in the future.
    def actions(self, state: S) -> Iterable[A]:
        '''All the actions allowed for the given state.

        This will be empty for terminal states.

        '''
        actions = self.mapping[state]
        return iter([]) if actions is None else actions.keys()

    def states(self) -> Iterable[S]:
        '''Iterate over all the states in this process—terminal *and*
        non-terminal.

        '''
        return self.mapping.keys()
