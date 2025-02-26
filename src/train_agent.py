"""
This code trains an RL agent that receives the LTL formula as input.
It is a simple adaptation of this repo: https://github.com/lcswillems/rl-starter-files
To run an agent, use the following command:
   >>> python train_agent.py --algo ppo --env Letter-7x7-v2 --model Letter --save-interval 10 --frames 1000000000
   >>> python train_agent.py --algo ppo --env Letter-7x7-v2 --model Test --save-interval 10 --procs 4 --frames 1000000000 --ltl-sampler UntilTasks_1_3_1_2
This runs PPO over the Letter-4x4-v0 environment. It saves the model *storage/Letter* and runs for 1000000000 frames.
NOTE:
    Letter-5x5-v0 -> Standard environment of 5x5 with a timeout of 150 steps
    Letter-5x5-v1 -> This version uses a fixed map of 5x5 with a timeout of 150 steps
    Letter-5x5-v2 -> Standard environment of 5x5 using an agent-centric view with a timeout of 150 steps
    Letter-5x5-v3 -> This version uses a fixed map of 5x5 using an agent-centric view with a timeout of 150 steps

    Letter-7x7-v0 -> Standard environment of 7x7 with a timeout of 500 steps
    Letter-7x7-v1 -> This version uses a fixed map of 7x7 with a timeout of 500 steps
    Letter-7x7-v2 -> Standard environment of 7x7 using an agent-centric view with a timeout of 500 steps
    Letter-7x7-v3 -> This version uses a fixed map of 7x7 using an agent-centric view with a timeout of 500 steps

BASELINES
==============
To run PPO while ignoring the LTL input, add the "--ignoreLTL" flag:
   >>> python train_agent.py --algo ppo --env Letter-4x4-v0 --model Letter --save-interval 10 --frames 1000000000 --ignoreLTL
To run PPO without progressing the LTL formula, add the "ignoreLTLprogression" flag:
    >>> python train_agent.py --algo ppo --env Letter-7x7-v2 --model Test --save-interval 10 --procs 4 --frames 1000000000 --ltl-sampler UntilTasks_1_3_1_2 --ignoreLTLprogression
To run PPO without progressing the LTL formula, but learn an LSTM policy use *--recurrence X*, where X > 1 (I think X=4 is a reasonable value)
    >>> python train_agent.py --algo ppo --env Letter-7x7-v2 --model Test --save-interval 10 --procs 4 --frames 1000000000 --ltl-sampler UntilTasks_1_3_1_2 --ignoreLTLprogression --recurrence 4
"""

import argparse
import time
import datetime
import torch
import torch_ac
import tensorboardX
import sys
import glob
from math import floor

import utils
from model import ACModel
from recurrent_model import RecurrentACModel
from envs.gym_letters.letter_env import LetterEnv

if __name__ == '__main__':

    # Parse arguments

    parser = argparse.ArgumentParser()

    ## General parameters
    parser.add_argument("--algo", required=True,
                        help="algorithm to use: a2c | ppo (REQUIRED)")
    parser.add_argument("--env", required=True,
                        help="name of the environment to train on (REQUIRED)")
    parser.add_argument("--ltl-sampler", default="Default",
                        help="the ltl formula template to sample from (default: DefaultSampler)")
    parser.add_argument("--model", default=None,
                        help="name of the model (default: {ENV}_{SAMPLER}_{ALGO}_{TIME})")
    parser.add_argument("--seed", type=int, default=1,
                        help="random seed (default: 1)")
    parser.add_argument("--log-interval", type=int, default=10,
                        help="number of updates between two logs (default: 10)")
    parser.add_argument("--save-interval", type=int, default=100,
                        help="number of updates between two saves (default: 10, 0 means no saving)")
    parser.add_argument("--procs", type=int, default=16,
                        help="number of processes (default: 16)")
    parser.add_argument("--frames", type=int, default=2*10**8,
                        help="number of frames of training (default: 2*10e8)")
    parser.add_argument("--checkpoint-dir", default=None)

    ## Evaluation parameters
    parser.add_argument("--eval", action="store_true", default=False,
                        help="evaluate the saved model (default: False)")
    parser.add_argument("--eval-episodes", type=int,  default=5,
                        help="number of episodes to evaluate on (default: 5)")
    parser.add_argument("--eval-env", default=None,
                        help="name of the environment to train on (default: use the same \"env\" as training)")
    parser.add_argument("--ltl-samplers-eval", default=None, nargs='+',
                        help="the ltl formula templates to sample from for evaluation (default: use the same \"ltl-sampler\" as training)")
    parser.add_argument("--eval-procs", type=int, default=1,
                        help="number of processes (default: use the same \"procs\" as training)")

    ## Parameters for main algorithm
    parser.add_argument("--epochs", type=int, default=4,
                        help="number of epochs for PPO (default: 4)")
    parser.add_argument("--batch-size", type=int, default=256,
                        help="batch size for PPO (default: 256)")
    parser.add_argument("--frames-per-proc", type=int, default=None,
                        help="number of frames per process before update (default: 5 for A2C and 128 for PPO)")
    parser.add_argument("--discount", type=float, default=0.99,
                        help="discount factor (default: 0.99)")
    parser.add_argument("--lr", type=float, default=0.0003,
                        help="learning rate (default: 0.0003)")
    parser.add_argument("--gae-lambda", type=float, default=0.95,
                        help="lambda coefficient in GAE formula (default: 0.95, 1 means no gae)")
    parser.add_argument("--entropy-coef", type=float, default=0.01,
                        help="entropy term coefficient (default: 0.01)")
    parser.add_argument("--value-loss-coef", type=float, default=0.5,
                        help="value loss term coefficient (default: 0.5)")
    parser.add_argument("--max-grad-norm", type=float, default=0.5,
                        help="maximum norm of gradient (default: 0.5)")
    parser.add_argument("--optim-eps", type=float, default=1e-8,
                        help="Adam and RMSprop optimizer epsilon (default: 1e-8)")
    parser.add_argument("--optim-alpha", type=float, default=0.99,
                        help="RMSprop optimizer alpha (default: 0.99)")
    parser.add_argument("--clip-eps", type=float, default=0.2,
                        help="clipping epsilon for PPO (default: 0.2)")
    parser.add_argument("--ignoreLTL", action="store_true", default=False,
                        help="the network ignores the LTL input")
    parser.add_argument("--noLTL", action="store_true", default=False,
                        help="the environment no longer has an LTL goal. --ignoreLTL must be specified concurrently.")
    parser.add_argument("--progression-mode", default="full",
                        help="Full: uses LTL progression; partial: shows the propositions which progress or falsify the formula; none: only original formula is seen. ")
    parser.add_argument("--recurrence", type=int, default=1,
                        help="number of time-steps gradient is backpropagated (default: 1). If > 1, a LSTM is added to the model to have memory.")
    parser.add_argument("--gnn", default="RGCN_8x32_ROOT_SHARED", help="use gnn to model the LTL (only if ignoreLTL==True)")
    parser.add_argument("--int-reward", type=float, default=0.0, help="the intrinsic reward for LTL progression (default: 0.0)")
    parser.add_argument("--pretrained-gnn", action="store_true", default=False, help="load a pre-trained LTL module.")
    parser.add_argument("--dumb-ac", action="store_true", default=False,help="Use a single-layer actor-critic")
    parser.add_argument("--freeze-ltl", action="store_true", default=False,help="Freeze the gradient updates of the LTL module")
    parser.add_argument("--use-dfa", action="store_true", default=False,help="Use DFA encoding of the LTL formula instead of AST")
    parser.add_argument("--use-mean-guard-embed", action="store_true", default=False,help="Use mean embeddings for DFA guards")
    parser.add_argument("--use-onehot-guard-embed", action="store_true", default=False,help="Use onehot embeddings for DFA guards")
    parser.add_argument("--give-mdp-state-to-gnn", action="store_true", default=False,help="Give the MDP state as and input to the GNN")

    args = parser.parse_args()

    use_mem = args.recurrence > 1

    # Set run dir

    date = datetime.datetime.now().strftime("%y-%m-%d-%H-%M-%S")

    gnn_name = args.gnn
    if args.ignoreLTL:
        gnn_name = "IgnoreLTL"
    if args.dumb_ac:
        gnn_name = gnn_name + "-dumb_ac"
    if args.pretrained_gnn:
        gnn_name = gnn_name + "-pretrained"
    if args.freeze_ltl:
        gnn_name = gnn_name + "-freeze_ltl"
    if use_mem:
        gnn_name = gnn_name + "-recurrence:%d"%(args.recurrence)

    default_model_name = f"{gnn_name}_{args.ltl_sampler}_{args.env}_seed:{args.seed}_epochs:{args.epochs}_bs:{args.batch_size}_fpp:{args.frames_per_proc}_dsc:{args.discount}_lr:{args.lr}_ent:{args.entropy_coef}_clip:{args.clip_eps}_prog:{args.progression_mode}_use-dfa:{args.use_dfa}_use_mean_guard_embed:{args.use_mean_guard_embed}_use_onehot_guard_embed:{args.use_onehot_guard_embed}_give_mdp_state_to_gnn:{args.give_mdp_state_to_gnn}"

    model_name = args.model or default_model_name
    storage_dir = "storage" if args.checkpoint_dir is None else args.checkpoint_dir
    model_dir = utils.get_model_dir(model_name, storage_dir)

    pretrained_model_dir = None

    if args.pretrained_gnn:
        assert(args.progression_mode == "full")
        default_dir = f"symbol-storage/{args.gnn}-dumb_ac_{args.ltl_sampler}_Simple-LTL-Env-v0_seed:{args.seed}_*_prog:{args.progression_mode}/train"
        print(default_dir)
        model_dirs = glob.glob(default_dir)
        if len(model_dirs) == 0:
            raise Exception("Pretraining directory not found.")
        elif len(model_dirs) > 1:
            raise Exception("More than 1 candidate pretraining directory found.")

        pretrained_model_dir = model_dirs[0]
    # Load loggers and Tensorboard writer

    txt_logger = utils.get_txt_logger(model_dir + "/train")
    csv_file, csv_logger = utils.get_csv_logger(model_dir + "/train")
    tb_writer = tensorboardX.SummaryWriter(model_dir + "/train")
    utils.save_config(model_dir + "/train", args)

    # Log command and all script arguments

    txt_logger.info("{}\n".format(" ".join(sys.argv)))
    txt_logger.info("{}\n".format(args))

    # Set seed for all randomness sources

    utils.seed(args.seed)

    # Set device

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    txt_logger.info(f"Device: {device}\n")

    # Load environments

    envs = []
    progression_mode = args.progression_mode
    for i in range(args.procs):
        envs.append(utils.make_env(args.env, progression_mode, args.ltl_sampler, args.seed, args.int_reward, args.noLTL, args.use_dfa))

    # Sync environments
    envs[0].reset()
    if isinstance(envs[0].env, LetterEnv):
        txt_logger.info("Using fixed maps.")
        for env in envs:
            env.env.map = envs[0].env.map

    txt_logger.info("Environments loaded\n")

    # Load training status

    try:
        status = utils.get_status(model_dir + "/train")
    except OSError:
        status = {"num_frames": 0, "update": 0}
    txt_logger.info("Training status loaded.\n")

    if pretrained_model_dir is not None:
        try:
            pretrained_status = utils.get_status(pretrained_model_dir)
        except:
            txt_logger.info("Failed to load pretrained model.\n")
            exit(1)

    # Load observations preprocessor
    using_gnn = (args.gnn != "GRU" and args.gnn != "LSTM")
    obs_space, preprocess_obss = utils.get_obss_preprocessor(envs[0], using_gnn, progression_mode, args.use_dfa, args.use_mean_guard_embed, args.use_onehot_guard_embed)
    if "vocab" in status and preprocess_obss.vocab is not None:
        preprocess_obss.vocab.load_vocab(status["vocab"])
    txt_logger.info("Observations preprocessor loaded.\n")

    # Load model
    if use_mem:
        acmodel = RecurrentACModel(envs[0].env, obs_space, envs[0].action_space, args.ignoreLTL, args.gnn, args.dumb_ac, args.freeze_ltl, args.use_dfa)
    else:
        acmodel = ACModel(envs[0].env, obs_space, envs[0].action_space, args.ignoreLTL, args.gnn, args.dumb_ac, args.freeze_ltl, args.use_dfa, args.give_mdp_state_to_gnn)
    if "model_state" in status:
        acmodel.load_state_dict(status["model_state"])
        txt_logger.info("Loading model from existing run.\n")

    elif args.pretrained_gnn:
        acmodel.load_pretrained_gnn(pretrained_status["model_state"])
        txt_logger.info("Pretrained model loaded.\n")

    acmodel.to(device)
    txt_logger.info("Model loaded.\n")
    txt_logger.info("{}\n".format(acmodel))

    # Load algo
    if args.algo == "a2c":
        algo = torch_ac.A2CAlgo(envs, acmodel, device, args.frames_per_proc, args.discount, args.lr, args.gae_lambda,
                                args.entropy_coef, args.value_loss_coef, args.max_grad_norm, args.recurrence,
                                args.optim_alpha, args.optim_eps, preprocess_obss)
    elif args.algo == "ppo":
        algo = torch_ac.PPOAlgo(envs, acmodel, device, args.frames_per_proc, args.discount, args.lr, args.gae_lambda,
                                args.entropy_coef, args.value_loss_coef, args.max_grad_norm, args.recurrence,
                                args.optim_eps, args.clip_eps, args.epochs, args.batch_size, preprocess_obss)
    else:
        raise ValueError("Incorrect algorithm name: {}".format(args.algo))

    if "optimizer_state" in status:
        algo.optimizer.load_state_dict(status["optimizer_state"])
        txt_logger.info("Loading optimizer from existing run.\n")
    txt_logger.info("Optimizer loaded.\n")

    # init the evaluator
    if args.eval:
        eval_samplers = args.ltl_samplers_eval if args.ltl_samplers_eval else [args.ltl_sampler]
        eval_env = args.eval_env if args.eval_env else args.env
        eval_procs = args.eval_procs if args.eval_procs else args.procs

        evals = []
        for eval_sampler in eval_samplers:
            evals.append(utils.Eval(eval_env, model_name, eval_sampler,
                        seed=args.seed, device=device, num_procs=eval_procs, ignoreLTL=args.ignoreLTL, progression_mode=progression_mode, gnn=args.gnn, dumb_ac = args.dumb_ac))


    # Train model

    num_frames = status["num_frames"]
    update = status["update"]
    start_time = time.time()

    while num_frames < args.frames:
        # Update model parameters
        update_start_time = time.time()
        exps, logs1 = algo.collect_experiences()
        logs2 = algo.update_parameters(exps)
        logs = {**logs1, **logs2}
        update_end_time = time.time()

        num_frames += logs["num_frames"]
        update += 1

        # Print logs

        if update % args.log_interval == 0:
            fps = logs["num_frames"]/(update_end_time - update_start_time)
            duration = int(time.time() - start_time)

            return_per_episode = utils.synthesize(logs["return_per_episode"])
            rreturn_per_episode = utils.synthesize(logs["reshaped_return_per_episode"])
            average_reward_per_step = utils.average_reward_per_step(logs["return_per_episode"], logs["num_frames_per_episode"])
            average_discounted_return = utils.average_discounted_return(logs["return_per_episode"], logs["num_frames_per_episode"], args.discount)
            num_frames_per_episode = utils.synthesize(logs["num_frames_per_episode"])

            header = ["update", "frames", "FPS", "duration"]
            data = [update, num_frames, fps, duration]
            header += ["rreturn_" + key for key in rreturn_per_episode.keys()]
            data += rreturn_per_episode.values()
            header += ["average_reward_per_step", "average_discounted_return"]
            data += [average_reward_per_step, average_discounted_return]
            header += ["num_frames_" + key for key in num_frames_per_episode.keys()]
            data += num_frames_per_episode.values()
            header += ["entropy", "value", "policy_loss", "value_loss", "grad_norm"]
            data += [logs["entropy"], logs["value"], logs["policy_loss"], logs["value_loss"], logs["grad_norm"]]

            txt_logger.info(
                "U {} | F {:06} | FPS {:04.0f} | D {} | rR:μσmM {:.2f} {:.2f} {:.2f} {:.2f} | ARPS: {:.3f} | ADR: {:.3f} | F:μσmM {:.1f} {:.1f} {} {} | H {:.3f} | V {:.3f} | pL {:.3f} | vL {:.3f} | ∇ {:.3f}"
                .format(*data))

            header += ["return_" + key for key in return_per_episode.keys()]
            data += return_per_episode.values()

            if status["num_frames"] == 0:
                csv_logger.writerow(header)
            csv_logger.writerow(data)
            csv_file.flush()

            for field, value in zip(header, data):
                tb_writer.add_scalar(field, value, num_frames)

        # Save status

        if args.save_interval > 0 and update % args.save_interval == 0:
            status = {"num_frames": num_frames, "update": update,
                      "model_state": algo.acmodel.state_dict(), "optimizer_state": algo.optimizer.state_dict()}
            if hasattr(preprocess_obss, "vocab") and preprocess_obss.vocab is not None:
                status["vocab"] = preprocess_obss.vocab.vocab
            utils.save_status(status, model_dir + "/train")
            txt_logger.info("Status saved")

            if args.eval:
                # we send the num_frames to align the eval curves with the training curves on TB
                for evalu in evals:
                    evalu.eval(num_frames, episodes=args.eval_episodes)

