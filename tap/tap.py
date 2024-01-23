import copy

import numpy as np

from tap.common import (
    conv_template,
    get_init_msg,
    process_target_response,
    random_string,
)
from tap.conversers import load_attack_and_target_models
from tap.evaluators import load_evaluator
from tap.loggers import WandBLogger
from tap.system_prompts import get_attacker_system_prompt


def clean_attacks_and_convs(attack_list, convs_list):
    """
    Remove any failed attacks (which appear as None) and corresponding conversations
    """
    tmp = [(a, c) for (a, c) in zip(attack_list, convs_list) if a is not None]
    tmp = [*zip(*tmp)]
    attack_list, convs_list = list(tmp[0]), list(tmp[1])

    return attack_list, convs_list


def prune(
    on_topic_scores=None,
    judge_scores=None,
    adv_prompt_list=None,
    improv_list=None,
    convs_list=None,
    target_response_list=None,
    extracted_attack_list=None,
    sorting_score=None,
    attack_params=None,
):
    """
    This function takes
        1. various lists containing metadata related to the attacks as input,
        2. a list with `sorting_score`
    It prunes all attacks (and correspondng metadata)
        1. whose `sorting_score` is 0;
        2. which exceed the `attack_params['width']` when arranged
           in decreasing order of `sorting_score`.

    In Phase 1 of pruning, `sorting_score` is a list of `on-topic` values.
    In Phase 2 of pruning, `sorting_score` is a list of `judge` values.
    """
    # Shuffle the brances and sort them according to judge scores
    shuffled_scores = enumerate(sorting_score)
    shuffled_scores = [(s, i) for (i, s) in shuffled_scores]
    # Ensures that elements with the same score are randomly permuted
    np.random.shuffle(shuffled_scores)
    shuffled_scores.sort(reverse=True)

    def get_first_k(list_):
        width = min(attack_params["width"], len(list_))

        truncated_list = [
            list_[shuffled_scores[i][1]]
            for i in range(width)
            if shuffled_scores[i][0] > 0
        ]

        # Ensure that the truncated list has at least two elements
        if len(truncated_list) == 0:
            truncated_list = [
                list_[shuffled_scores[0][0]],
                list_[shuffled_scores[0][1]],
            ]

        return truncated_list

    # Prune the brances to keep
    # 1) the first attack_params['width']-parameters
    # 2) only attacks whose score is positive

    if judge_scores is not None:
        judge_scores = get_first_k(judge_scores)

    if target_response_list is not None:
        target_response_list = get_first_k(target_response_list)

    on_topic_scores = get_first_k(on_topic_scores)
    adv_prompt_list = get_first_k(adv_prompt_list)
    improv_list = get_first_k(improv_list)
    convs_list = get_first_k(convs_list)
    extracted_attack_list = get_first_k(extracted_attack_list)

    return (
        on_topic_scores,
        judge_scores,
        adv_prompt_list,
        improv_list,
        convs_list,
        target_response_list,
        extracted_attack_list,
    )


class TAP:
    def __init__(self, args):
        self.args = args
        self.exp_mode = args.exp_mode
        self.attack_params = {
            "width": args.width,
            "branching_factor": args.branching_factor,
            "depth": args.depth,
        }
        self.n_streams = args.n_streams
        self.keep_last_n = args.keep_last_n
        self.attack_llm, self.target_llm = load_attack_and_target_models(args)
        print("Done loading attacker and target!", flush=True)
        self.evaluator_llm = load_evaluator(args)
        print("Done loading evaluator!", flush=True)

    def run(self, goal: str, target_str: str, **kwargs):
        # Initialize models and logger
        system_prompt = get_attacker_system_prompt(
            self.exp_mode, goal, target_str, **kwargs
        )
        logger = WandBLogger(self.args, system_prompt)
        print("Done logging!", flush=True)
        original_prompt = goal

        # Initialize conversations
        batchsize = self.n_streams
        init_msg = get_init_msg(goal, target_str, self.exp_mode)
        processed_response_list = [init_msg for _ in range(batchsize)]

        # FIXME: Alpaca conversation template
        convs_list = [
            conv_template(
                self.attack_llm.template, self_id="NA", parent_id="NA"
            )
            for _ in range(batchsize)
        ]

        for conv in convs_list:
            conv.set_system_message(system_prompt)

        # Begin TAP

        print("Beginning TAP!", flush=True)

        for iteration in range(1, self.attack_params["depth"] + 1):
            print(
                f"""\n{'='*36}\nTree-depth is: {iteration}\n{'='*36}\n""",
                flush=True,
            )

            ############################################################
            #   BRANCH
            ############################################################
            extracted_attack_list = []
            convs_list_new = []

            for _ in range(self.attack_params["branching_factor"]):
                print(f"Entering branch number {_}", flush=True)
                convs_list_copy = copy.deepcopy(convs_list)

                for c_new, c_old in zip(convs_list_copy, convs_list):
                    c_new.self_id = random_string(32)
                    c_new.parent_id = c_old.self_id

                extracted_attack_list.extend(
                    self.attack_llm.get_attack(
                        convs_list_copy, processed_response_list
                    )
                )
                convs_list_new.extend(convs_list_copy)

            # Remove any failed attacks and corresponding conversations
            convs_list = copy.deepcopy(convs_list_new)
            extracted_attack_list, convs_list = clean_attacks_and_convs(
                extracted_attack_list, convs_list
            )

            adv_prompt_list = [
                attack["prompt"] for attack in extracted_attack_list
            ]
            improv_list = [
                attack["improvement"] for attack in extracted_attack_list
            ]

            if self.exp_mode == "ignore":
                inst = kwargs["inst"]
                inpt = kwargs["input"]
                for i, prompt in enumerate(adv_prompt_list):
                    # FIXME: format (need ### Instruction and ### Response?)
                    new_prompt = f"{inst} ### Input: {inpt} {prompt} {goal}"
                    adv_prompt_list[i] = new_prompt

            print("DEBUG")
            print(">>> adv_prompt_list:", adv_prompt_list[0])
            print(">>> improv_list:", improv_list[0])

            ############################################################
            #   PRUNE: PHASE 1
            ############################################################
            # Get on-topic-scores (does the adv_prompt asks for same info as original prompt)
            on_topic_scores = self.evaluator_llm.on_topic_score(
                adv_prompt_list, original_prompt
            )

            # Prune attacks which are irrelevant
            (
                on_topic_scores,
                _,
                adv_prompt_list,
                improv_list,
                convs_list,
                _,
                extracted_attack_list,
            ) = prune(
                on_topic_scores,
                None,  # judge_scores
                adv_prompt_list,
                improv_list,
                convs_list,
                None,  # target_response_list
                extracted_attack_list,
                sorting_score=on_topic_scores,
                attack_params=self.attack_params,
            )

            print(
                f"Total number of prompts (after pruning phase 1) are {len(adv_prompt_list)}"
            )

            ############################################################
            #   QUERY AND ASSESS
            ############################################################
            target_response_list = self.target_llm.get_response(adv_prompt_list)
            print("Finished getting target responses.")

            # Get judge-scores (i.e., likelihood of jailbreak) from Evaluator
            judge_scores = self.evaluator_llm.judge_score(
                adv_prompt_list, target_response_list
            )
            print("Finished getting judge scores from evaluator.")

            ############################################################
            #   PRUNE: PHASE 2
            ############################################################
            # Prune attacks which to be fewer than attack_params['width']
            (
                on_topic_scores,
                judge_scores,
                adv_prompt_list,
                improv_list,
                convs_list,
                target_response_list,
                extracted_attack_list,
            ) = prune(
                on_topic_scores,
                judge_scores,
                adv_prompt_list,
                improv_list,
                convs_list,
                target_response_list,
                extracted_attack_list,
                sorting_score=judge_scores,
                attack_params=self.attack_params,
            )

            # WandB log values
            logger.log(
                iteration,
                extracted_attack_list,
                target_response_list,
                judge_scores,
                on_topic_scores,
                conv_ids=[c.self_id for c in convs_list],
                parent_conv_ids=[c.parent_id for c in convs_list],
            )

            # Truncate conversation to avoid context length issues
            for conv in convs_list:
                # Note that this does not delete the conv.role (i.e., the system prompt)
                conv.messages = conv.messages[-2 * (self.keep_last_n) :]

            print(">>> adv_prompt_list:", adv_prompt_list[0])
            print(">>> target_response_list:", target_response_list[0])

            # Early stopping criterion
            if any([score == 10 for score in judge_scores]):
                print("Found a jailbreak. Exiting.")
                break

            # `process_target_response` concatenates the target response, goal, and score
            #   -- while adding appropriate labels to each
            processed_response_list = [
                process_target_response(
                    target_response=target_response,
                    score=score,
                    goal=goal,
                    target_str=target_str,
                )
                for target_response, score in zip(
                    target_response_list, judge_scores
                )
            ]

        logger.finish()
