#!/usr/bin/env python

# Copyright (c) Facebook, Inc. and its affiliates.
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

"""
Agents for handling the generation aspects of Wizard.
"""
from itertools import chain
from functools import lru_cache

import torch as th
import numpy as np

from parlai.core.torch_agent import Batch as TABatch
from parlai.core.metrics import _f1_score
from parlai.core.utils import padded_tensor, round_sigfigs

from parlai.agents.transformer.transformer import TransformerGeneratorAgent

from .modules import EndToEndModel
from parlai.tasks.wizard_of_wikipedia.agents import TOKEN_KNOWLEDGE

TOKEN_DIALOG = '__dialog__'


# extend the default batch
class Batch(TABatch):
    def __init__(
        self,
        # needed for evaluation metrics
        checked_sentence=None,
        # needed for model
        cs_ids=None,
        know_vec=None,
        ck_mask=None,
        use_cs_ids=None,
        knowledge=None,
        **kwargs,
    ):
        super().__init__(
            checked_sentence=checked_sentence,
            cs_ids=cs_ids,
            know_vec=know_vec,
            ck_mask=ck_mask,
            use_cs_ids=use_cs_ids,
            knowledge=knowledge,
            **kwargs,
        )


DEFAULT_OPTS = {
    "learningrate": 3e-4,
    "optimizer": "adam",
    "lr_scheduler": "invsqrt",
    "warmup_updates": 1000,
    "clip_norm": 0.1,
    "ffn_size": 512,
    "embedding_size": 256,
    "n_heads": 2,
    "relu_dropout": 0.2,
    "attention_dropout": 0.2,
    "n_layers": 3,
    "truncate": 128,
    "dict_textfields": "text,labels,chosen_topic,checked_sentence,knowledge,title",
}


class _GenericWizardAgent(TransformerGeneratorAgent):
    # @classmethod
    # def dictionary_class(cls):
    #     import parlai_internal.projects.wizard.generator.agents as a
    #     return a._WizardGeneratorDictionary

    # todo:
    # - dict agent stuff
    @classmethod
    def add_cmdline_args(cls, argparser):
        argparser.set_defaults(**DEFAULT_OPTS)
        super(_GenericWizardAgent, cls).add_cmdline_args(argparser)

    def batchify(self, obs_batch):
        batch = super().batchify(obs_batch)
        reordered_observations = [obs_batch[i] for i in batch.valid_indices]

        checked_sentences = []
        for obs in reordered_observations:
            checked_sentence = '{} {} {}'.format(
                obs.get('title', ''), TOKEN_KNOWLEDGE, obs.get('checked_sentence', '')
            )
            checked_sentences.append(checked_sentence)

        batch['checked_sentence'] = checked_sentences
        return batch


class TwoStageAgent(_GenericWizardAgent):
    def observe(self, obs):
        if 'text' not in obs:
            return obs

        # TODO: resolve this with #1421
        # get the dialog stuff
        reply = self.last_reply()
        self.observation = self.get_dialog_history(obs, reply=reply)
        # we need to store the old text so that we can restore it
        oldtext = obs['text']

        # now we want to force prepend the knowledge stuff
        fields = []
        if 'chosen_topic' in obs:
            fields += [obs['title']]
        if 'checked_sentence' in obs:
            fields += [TOKEN_KNOWLEDGE, obs['checked_sentence']]
        if obs['text'] != '':
            fields += [TOKEN_DIALOG, obs['text']]
        obs['text'] = ' '.join(fields)

        # now vectorize with the extra knowledge. It'll all get stored in the
        # text_vec operation, etc
        self.vectorize(
            obs,
            text_truncate=self.text_truncate,
            label_truncate=self.label_truncate
        )

        # finally we need to return the old text to the way it was
        obs['text'] = oldtext
        assert obs is self.observation

        return obs


class EndToEndAgent(_GenericWizardAgent):
    def __init__(self, opt, shared=None):
        super().__init__(opt, shared)
        self._vectorize_text = lru_cache(int(2 ** 20))(self._vectorize_text)

        # knowledge truncate defaults to the same as --truncate
        self.knowledge_truncate = opt.get('knowledge_truncate')
        if not self.knowledge_truncate:
            self.knowledge_truncate = opt['truncate']
        self.max_knowledge = opt.get('max_knowledge')
        self.knowledge_alpha = opt['knowledge_alpha']

    def _dummy_batch(self, bsz, maxlen):
        batch = super()._dummy_batch(bsz, maxlen)
        batch['know_vec'] = th.zeros(bsz, 2, 2).long().cuda()
        batch['ck_mask'] = th.ones(bsz, 2).byte().cuda()
        batch['cs_ids'] = th.zeros(bsz).long().cuda()
        batch['use_cs_ids'] = True
        return batch

    def compute_loss(self, batch, return_output=False):
        # first compute our regular forced decoding loss
        token_loss, model_output = super().compute_loss(batch, return_output=True)

        encoder_states = model_output[2]
        ctx_know_attn = encoder_states[2]

        if self.knowledge_alpha == 0.0:
            loss = token_loss
        else:
            _, know_pred = ctx_know_attn.max(1)
            know_acc = (know_pred == batch.cs_ids).float().sum().item()
            know_chance = batch.ck_mask.sum(1).float().reciprocal().sum().item()
            self.metrics['know_chance'] += know_chance
            self.metrics['bsz'] += batch.text_vec.size(0)
            self.metrics['know_acc'] += know_acc
            know_loss = th.nn.functional.cross_entropy(
                ctx_know_attn,
                batch.cs_ids,
                reduction='mean',
            )
            self.metrics['know_loss'] += know_loss.item() * batch.text_vec.size(0)
            loss = (
                (1 - self.knowledge_alpha) * token_loss +
                self.knowledge_alpha * know_loss
            )
        if return_output:
            return (loss, model_output)
        else:
            return loss

    def reset_metrics(self):
        super().reset_metrics()
        self.metrics['bsz'] = 0.0
        self.metrics['know_acc'] = 0.0
        self.metrics['know_loss'] = 0.0
        self.metrics['know_chance'] = 0.0

    def report(self):
        r = super().report()
        bsz = max(self.metrics['bsz'], 1)
        for k in ['know_loss', 'know_acc', 'know_chance']:
            # round and average across all items since last report
            self.metrics[k] = round_sigfigs(self.metrics[k] / bsz, 4)
        return r

    def _parse_knowledge(self, obs):
        if 'knowledge_parsed' in obs:
            # make a copy of the list to prevent the future padding step from
            # being destructive
            return list(obs['knowledge_parsed'])

        checked_sentence = '{} {} {}'.format(
            obs['title'], TOKEN_KNOWLEDGE, obs['checked_sentence']
        )
        # grab all the nonempty knowledge
        obs_know = [k.strip() for k in obs.get('knowledge', '').split('\n')]
        obs_know = [k for k in obs_know if k]

        # we want the correct knowledge to always be in index 0
        try:
            i = obs_know.index(checked_sentence)
        except ValueError:
            # uh oh, couldn't find the sentence in the knowledge. This happens for
            # one or two examples in the training set. We can just artificially
            # put it back in
            i = 0
            obs_know[0] = checked_sentence
        obs_know[0], obs_know[i] = obs_know[i], obs_know[0]

        obs['knowledge_parsed'] = obs_know
        obs['checked_sentence_parsed'] = checked_sentence
        return obs['knowledge_parsed']

    def batchify(self, obs_batch):
        """
        Wizard custom batchify, which passes along the knowledge/title.

        Following the docstring of TorchAgent.batchify, it calls super, then
        uses an extended version of the torch_agent.Batch namedtuple.

        The purpose of extending the info is to keep track of some custom
        metrics.
        """
        batch = super().batchify(obs_batch)
        reordered_observations = [obs_batch[i] for i in batch.valid_indices]
        is_training = not ('eval_labels' in reordered_observations[0])

        # first parse and compile all the knowledge together
        all_knowledges = []  # list-of-lists knowledge items for each observation
        knowledge_counts = []  # how much knowledge each observation gets
        for obs in reordered_observations:
            obs_know = self._parse_knowledge(obs)
            # downsample if desired
            if (is_training and self.max_knowledge and
                    len(obs_know) > self.max_knowledge):
                # offset by one so that we don't choose 0
                keepers = 1 + np.random.choice(
                    len(obs_know) - 1, self.max_knowledge, False
                )
                # correct answer is always the first one
                keepers[0] = 0
                obs_know = [obs_know[i] for i in keepers]
            all_knowledges.append(obs_know)
            knowledge_counts.append(len(obs_know))

        # now we want to actually pack this into a tensor, along with the mask
        N = len(reordered_observations)
        K = max(knowledge_counts)
        # round out the array so everything is equally sized
        for i in range(N):
            all_knowledges[i] += [''] * (K - knowledge_counts[i])
        flattened_knowledge = list(chain(*all_knowledges))

        knowledge_vec = [
            self._vectorize_text(
                # the beginning of the sentence is more useful
                k, truncate=self.knowledge_truncate, add_end=True, truncate_left=False
            )
            for k in flattened_knowledge
        ]
        knowledge_vec, _ = padded_tensor(
            knowledge_vec, self.NULL_IDX, self.use_cuda, left_padded=False,
        )
        T = knowledge_vec.size(-1)
        knowledge_vec = knowledge_vec.view(N, K, T)

        # knowledge mask is a N x K tensor saying which items we're allowed to
        # attend over
        bsz = len(reordered_observations)
        ck_mask = th.zeros(bsz, K).byte()
        for i, klen in enumerate(knowledge_counts):
            ck_mask[i, :klen] = 1
        # and the correct labels
        cs_ids = th.LongTensor(bsz).zero_()

        if self.use_cuda:
            knowledge_vec = knowledge_vec.cuda()
            ck_mask = ck_mask.cuda()
            cs_ids = cs_ids.cuda()

        batch['know_vec'] = knowledge_vec
        batch['ck_mask'] = ck_mask
        batch['cs_ids'] = cs_ids
        batch['use_cs_ids'] = is_training
        batch['knowledge'] = np.array(flattened_knowledge).reshape(N, K)
        return batch

    @classmethod
    def add_cmdline_args(cls, argparser):
        super(EndToEndAgent, cls).add_cmdline_args(argparser)
        group = argparser.add_argument_group("EndToEnd Agent")
        group.add_argument(
            '--knowledge-alpha', type=float, default=0.95,
            help='Weight on the knowledge-attn loss'
        )
        group.add_argument(
            '--knowledge-truncate', type=int, default=32,
            help='Knowledge truncation field. Defaults to same as --truncate.'
        )
        group.add_argument(
            '--max-knowledge', type=int,
            help='Reduce the amount of negative knowledge at train time.'
        )
        argparser.add_argument(
            '--knowledge-alpha', type=float, default=0.95,
            help='Weight on the knowledge-attn loss'
        )

    def _model_input(self, batch):
        return (
            batch.text_vec,
            batch.know_vec,
            batch.ck_mask,
            batch.cs_ids,
            batch.use_cs_ids,
        )

    def eval_step(self, batch):
        predictions = super().eval_step(batch)
        if not hasattr(self, 'know_pred'):
            # interactive or mturk mode; there's no gold label.
            return predictions

        # measure overlap with original knowledge decision
        for cs, kp, knowledges in zip(batch.cs_ids, self.know_pred, batch.knowledge):
            gold = knowledges[cs.item()]
            chosen = knowledges[kp.item()]
            self.metrics['cs_f1'].append(_f1_score(
                chosen.replace(TOKEN_KNOWLEDGE, ''),
                [gold.replace(TOKEN_KNOWLEDGE, '')]
            ))

        return predictions

    def build_model(self):
        self.model = EndToEndModel(self.opt, self.dict)
        if self.opt['embedding_type'] != 'random':
            self._copy_embeddings(
                self.model.embeddings.weight, self.opt['embedding_type']
            )
        if self.use_cuda:
            self.model = self.model.cuda()
        return self.model
