import os
import time
import copy
import vegas
import logging
import torch
import torch.nn as nn
import numpy as np
import torch.utils.data as data
from math import exp as exp
from sklearn.metrics import roc_auc_score
from scipy import integrate

from CAT.model.abstract_model import AbstractModel
from CAT.dataset import AdapTestDataset, TrainDataset, Dataset


class IRT(nn.Module):
    def __init__(self, num_students, num_questions, num_dim):
        # num_dim: IRT if num_dim == 1 else MIRT
        super().__init__()
        self.num_dim = num_dim
        self.num_students = num_students
        self.num_questions = num_questions
        self.theta = nn.Embedding(self.num_students, self.num_dim)
        self.alpha = nn.Embedding(self.num_questions, self.num_dim)
        self.beta = nn.Embedding(self.num_questions, 1)

        for name, param in self.named_parameters():
            if 'weight' in name:
                nn.init.xavier_normal_(param)

    def forward(self, student_ids, question_ids):
        theta = self.theta(student_ids)
        alpha = self.alpha(question_ids)
        beta = self.beta(question_ids)
        pred = (alpha * theta).sum(dim=1, keepdim=True) + beta
        pred = torch.sigmoid(pred)
        return pred


class IRTModel(AbstractModel):

    def __init__(self, **config):
        super().__init__()
        self.config = config
        self.model = None

    @property
    def name(self):
        return 'Item Response Theory'

    def init_model(self, data: Dataset):
        self.model = IRT(data.num_students, data.num_questions, self.config['num_dim'])
    
    def train(self, train_data: TrainDataset, log_step=1):
        lr = self.config['learning_rate']
        batch_size = self.config['batch_size']
        epochs = self.config['num_epochs']
        device = self.config['device']
        self.model.to(device)
        logging.info('train on {}'.format(device))

        train_loader = data.DataLoader(train_data, batch_size=batch_size, shuffle=True)
        optimizer = torch.optim.Adam(self.model.parameters(), lr=lr)

        for ep in range(1, epochs + 1):
            loss = 0.0
            for cnt, (student_ids, question_ids, _, labels) in enumerate(train_loader):
                student_ids = student_ids.to(device)
                question_ids = question_ids.to(device)
                labels = labels.to(device).float()
                pred = self.model(student_ids, question_ids).view(-1)
                bz_loss = self._loss_function(pred, labels)
                optimizer.zero_grad()
                bz_loss.backward()
                optimizer.step()
                loss += bz_loss.data.float()
                if cnt % log_step == 0:
                    logging.info('Epoch [{}] Batch [{}]: loss={:.5f}'.format(ep, cnt, loss / cnt))

    def adaptest_save(self, path):
        """
        Save the model. Only save the parameters of questions(alpha, beta)
        """
        model_dict = self.model.state_dict()
        model_dict = {k:v for k,v in model_dict.items() if 'alpha' in k or 'beta' in k}
        torch.save(model_dict, path)

    def adaptest_load(self, path):
        """
        Reload the saved model
        """
        self.model.load_state_dict(torch.load(path), strict=False)
        self.model.to(self.config['device'])

    def adaptest_update(self, adaptest_data: AdapTestDataset):
        """
        Update CDM with tested data
        """
        lr = self.config['learning_rate']
        batch_size = self.config['batch_size']
        epochs = self.config['num_epochs']
        device = self.config['device']
        optimizer = torch.optim.Adam(self.model.theta.parameters(), lr=lr)

        tested_dataset = adaptest_data.get_tested_dataset(last=True)
        dataloader = torch.utils.data.DataLoader(tested_dataset, batch_size=batch_size, shuffle=True)

        for ep in range(1, epochs + 1):
            loss = 0.0
            log_steps = 100
            for cnt, (student_ids, question_ids, _, labels) in enumerate(dataloader):
                student_ids = student_ids.to(device)
                question_ids = question_ids.to(device)
                labels = labels.to(device).float()
                pred = self.model(student_ids, question_ids).view(-1)
                bz_loss = self._loss_function(pred, labels)
                optimizer.zero_grad()
                bz_loss.backward()
                optimizer.step()
                loss += bz_loss.data.float()
                # if cnt % log_steps == 0:
                    # print('Epoch [{}] Batch [{}]: loss={:.3f}'.format(ep, cnt, loss / cnt))
    
    def evaluate(self, adaptest_data: AdapTestDataset):
        data = adaptest_data.data
        concept_map = adaptest_data.concept_map
        device = self.config['device']

        real = []
        pred = []
        with torch.no_grad():
            self.model.eval()
            for sid in data:
                student_ids = [sid] * len(data[sid])
                question_ids = list(data[sid].keys())
                real += [data[sid][qid] for qid in question_ids]
                student_ids = torch.LongTensor(student_ids).to(device)
                question_ids = torch.LongTensor(question_ids).to(device)
                output = self.model(student_ids, question_ids).view(-1)
                pred += output.tolist()
            self.model.train()

        coverages = []
        for sid in data:
            all_concepts = set()
            tested_concepts = set()
            for qid in data[sid]:
                all_concepts.update(set(concept_map[qid]))
            for qid in adaptest_data.tested[sid]:
                tested_concepts.update(set(concept_map[qid]))
            coverage = len(tested_concepts) / len(all_concepts)
            coverages.append(coverage)
        cov = sum(coverages) / len(coverages)

        real = np.array(real)
        pred = np.array(pred)
        auc = roc_auc_score(real, pred)

        return {
            'auc': auc,
            'cov': cov,
        }

    def _loss_function(self, pred, real):
        return -(real * torch.log(0.0001 + pred) + (1 - real) * torch.log(1.0001 - pred)).mean()
    
    def get_alpha(self, question_id):
        """ get alpha of one question
        Args:
            question_id: int, question id
        Returns:
            alpha of the given question
        """
        return self.model.alpha.weight.data.numpy()[question_id]
    
    def get_beta(self, question_id):
        """ get beta of one question
        Args:
            question_id: int, question id
        Returns:
            beta of the given question
        """
        return self.model.beta.weight.data.numpy()[question_id]
    
    def get_theta(self, student_id):
        """ get theta of one student
        Args:
            student_id: int, student id
        Returns:
            theta of the given student
        """
        return self.model.theta.weight.data.numpy()[student_id]

    def get_kli(self, student_id, question_id, n):
        """ get KL information
        Args:
            student_id: int, student id
            question_id: int, question id
            n: int, the number of iteration
        Returns:
            v: float, KL information
        """
        if n == 0:
            return np.inf
        device = self.config['device']
        dim = self.model.num_dim
        sid = torch.LongTensor([student_id]).to(device)
        qid = torch.LongTensor([question_id]).to(device)
        theta = self.model.theta(sid).clone().detach().numpy()[0] # (num_dim, )
        alpha = self.model.alpha(qid).clone().detach().numpy()[0] # (num_dim, )
        beta = self.model.beta(qid).clone().detach().numpy()[0][0] # float value
        pred_estimate = self.model(sid, qid).data.numpy()[0][0] # float value
        def kli(x):
            """ The formula of KL information. Used for integral.
            Args:
                x: theta of student sid
            """
            if type(x) == float:
                x = np.array([x])
            pred = np.matmul(alpha.T, x) + beta
            pred = 1 / (1 + np.exp(-pred))
            q_estimate = 1  - pred_estimate
            q = 1 - pred
            return pred_estimate * np.log(pred_estimate / pred) + \
                    q_estimate * np.log((q_estimate / q))
        c = 3
        boundaries = [[theta[i] - c / np.sqrt(n), theta[i] + c / np.sqrt(n)] for i in range(dim)]
        if len(boundaries) == 1:
            v, err = integrate.quad(kli, boundaries[0][0], boundaries[0][1])
            return v
        integ = vegas.Integrator(boundaries)
        result = integ(kli, nitn=10, neval=1000)
        return result.mean

    def get_fisher(self, student_id, question_id):
        """ get Fisher information
        Args:
            student_id: int, student id
            question_id: int, question id
        Returns:
            fisher_info: matrix(num_dim * num_dim), Fisher information
        """
        device = self.config['device']
        sid = torch.LongTensor([student_id]).to(device)
        qid = torch.LongTensor([question_id]).to(device)
        alpha = self.model.alpha(qid).clone().detach()
        pred = self.model(sid, qid).data
        q = 1 - pred
        fisher_info = (q*pred*(alpha * alpha.T)).numpy()
        return fisher_info
    
    def expected_model_change(self, sid: int, qid: int, adaptest_data: AdapTestDataset):
        """ get expected model change
        Args:
            student_id: int, student id
            question_id: int, question id
        Returns:
            float, expected model change
        """
        epochs = self.config['num_epochs']
        lr = self.config['learning_rate']
        device = self.config['device']
        optimizer = torch.optim.Adam(self.model.parameters(), lr=lr)

        for name, param in self.model.named_parameters():
            if 'theta' not in name:
                param.requires_grad = False

        original_weights = self.model.theta.weight.data.clone()

        student_id = torch.LongTensor([sid]).to(device)
        question_id = torch.LongTensor([qid]).to(device)
        correct = torch.LongTensor([1]).to(device).float()
        wrong = torch.LongTensor([0]).to(device).float()

        for ep in range(epochs):
            optimizer.zero_grad()
            pred = self.model(student_id, question_id)
            loss = self._loss_function(pred, correct)
            loss.backward()
            optimizer.step()

        pos_weights = self.model.theta.weight.data.clone()
        self.model.theta.weight.data.copy_(original_weights)

        for ep in range(epochs):
            optimizer.zero_grad()
            pred = self.model(student_id, question_id)
            loss = self._loss_function(pred, wrong)
            loss.backward()
            optimizer.step()

        neg_weights = self.model.theta.weight.data.clone()
        self.model.theta.weight.data.copy_(original_weights)

        for param in self.model.parameters():
            param.requires_grad = True

        pred = self.model(student_id, question_id).item()
        return pred * torch.norm(pos_weights - original_weights).item() + \
               (1 - pred) * torch.norm(neg_weights - original_weights).item()
        


