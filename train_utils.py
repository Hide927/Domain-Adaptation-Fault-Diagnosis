import utils
import models
import data_loader
from models import Man_Moe
from models import MSTLN
import os
import torch
import logging
import itertools
import torch.nn as nn
from tqdm import tqdm
from torch import optim
import torch.nn.functional as F
from collections import defaultdict

class train_utils(object):
    
    def __init__(self, args, save_dir):
        self.args = args
        self.save_dir = save_dir
        if args.cuda_device:
            self.device = torch.device("cuda")
            logging.info('using {} gpus'.format(torch.cuda.device_count()))
        else:
            self.device = torch.device("cpu")
            logging.info('using cpu')
        self.num_source = len(args.source_name)
    
    def _get_lr_scheduler(self, optimizer):
        '''
        Get learning rate scheduler for optimizer.
        '''
        args = self.args
        
        # Define the learning rate decay
        if args.lr_scheduler == 'step':
            steps = [int(step) for step in args.steps.split(',')]
            lr_scheduler = optim.lr_scheduler.MultiStepLR(optimizer, steps, gamma=args.gamma)
        elif args.lr_scheduler == 'exp':
            lr_scheduler = optim.lr_scheduler.ExponentialLR(optimizer, args.gamma)
        elif args.lr_scheduler == 'stepLR':
            steps = int(args.steps)
            lr_scheduler = optim.lr_scheduler.StepLR(optimizer, steps, args.gamma)
        elif args.lr_scheduler == 'fix':
            lr_scheduler = None
        else:
            raise Exception("lr schedule not implemented")
            
        return lr_scheduler
    
    def _get_optimizer(self, model):
        '''
        Get optimizer for model.
        '''
        args = self.args
        if type(model) == list:
            par = filter(lambda p: p.requires_grad, itertools.chain(*map(list,
                                        [md.parameters() for md in model])))
        else:
            par = model.parameters()
        
        # Define the optimizer
        if args.opt == 'sgd':
            optimizer = optim.SGD(par, lr=args.lr, momentum=args.momentum,
                                  weight_decay=args.weight_decay)
        elif args.opt == 'adam':
            optimizer = optim.Adam(par, lr=args.lr, weight_decay=args.weight_decay)
        else:
            raise Exception("optimizer not implemented")
            
        return optimizer
        
    def _init_data(self):
        '''
        Initialize the datasets.
        '''
        args = self.args
        
        self.datasets = {}
        for source in args.source_name:
            data_root = os.path.join(args.data_dir, source)
            try:
                Dataset = getattr(data_loader, source)
            except:
                raise Exception("data name type not implemented")
            self.datasets[source] = Dataset(data_root, args.normlizetype).data_preprare(is_src=True)
            logging.info('source set {} length {}.'.format(source, len(self.datasets[source])))
            self.datasets[source].summary()
        
        data_root = os.path.join(args.data_dir, args.target_name)
        try:
            Dataset = getattr(data_loader, args.target_name)
        except:
            raise Exception("data name type not implemented")
        self.datasets['train'], self.datasets['val'] = Dataset(data_root,
                                                               args.normlizetype).data_preprare()
        logging.info('training set length {}, validation set length {}.'.format(
                                         len(self.datasets['train']), len(self.datasets['val'])))
        self.datasets['train'].summary(); self.datasets['val'].summary()
                 
        self.dataloaders = {x: torch.utils.data.DataLoader(self.datasets[x],
                                             batch_size=args.batch_size,
                                             shuffle=(False if x == 'val' else True),
                                             num_workers=args.num_workers, drop_last=True,
                                             pin_memory=(True if self.device == 'cuda' else False))
                                             for x in (['train', 'val'] + args.source_name)}
        self.iters = {x: iter(self.dataloaders[x]) for x in (['train', 'val'] + args.source_name)}
        
    def train_single_src(self):
        args = self.args
        assert self.num_source == 1
        src = args.source_name[0]
        self._init_data()
        
        model = getattr(models, args.model_name)
        if args.model_name in ['CNN', 'WDCNN']:
            training_mode = 0
            self.model = model(in_channel=1, num_classes=3).to(self.device)
        elif args.model_name in ['DAN', 'DANN', 'CDAN', 'ACDANN']:
            training_mode = 1
            self.model = model(in_channel=1, num_classes=3).to(self.device)
        elif args.model_name in ['ADACL', 'MFSAN']:
            training_mode = 2
            self.model = model(in_channel=1, num_classes=3, num_source=1).to(self.device)
        elif args.model_name in ['MADN', 'MSSA']:
            training_mode = 3
            self.model = model(in_channel=1, num_classes=3, num_source=1).to(self.device)
        else:
            raise Exception("model type not implemented")
    
        self.optimizer = self._get_optimizer(self.model)
        self.lr_scheduler = self._get_lr_scheduler(self.optimizer)
        
        best_acc = 0.0
        best_epoch = 0
   
        for epoch in range((args.max_epoch+1)):
            logging.info('-'*5 + 'Epoch {}/{}'.format(epoch, args.max_epoch) + '-'*5)
            
            # Update the learning rate
            if self.lr_scheduler is not None:
                logging.info('current lr: {}'.format(self.lr_scheduler.get_last_lr()))
   
            # Each epoch has a training and val phase
            for phase in ['train', 'val']:
                epoch_acc = 0
                epoch_loss = defaultdict(float)
   
                # Set model to train mode or evaluate mode
                if phase == 'train':
                    self.model.train()
                else:
                    self.model.eval()
                
                num_iter = len(self.iters[phase])
                for i in tqdm(range(num_iter), ascii=True):
                    if phase == 'train' or training_mode == 3:
                        source_data, source_labels = utils.get_next_batch(self.dataloaders,
                    						     self.iters, src, self.device)
                    if training_mode > 0 or phase == 'val':
                        target_data, target_labels = utils.get_next_batch(self.dataloaders,
                    						     self.iters, phase, self.device)
                    
                    if phase == 'train':
                        with torch.set_grad_enabled(True):
                            # forward
                            self.optimizer.zero_grad()
                            if training_mode == 0:
                                pred, loss = self.model(source_data, source_labels)
                                epoch_loss[0] += loss
                            elif training_mode == 1:
                                pred, loss_0, loss_1 = self.model(target_data,
                                                        source_data, source_labels)
                                loss = loss_0 + args.tradeoff[0] * loss_1
                                epoch_loss[0] += loss_0; epoch_loss[1] += loss_1
                            elif training_mode == 2:
                                pred, loss_0, loss_1, loss_2 = self.model(target_data,
                                             source_data, source_labels, source_idx=0)
                                loss = loss_0 + args.tradeoff[0] * loss_1 + \
                                                     args.tradeoff[1] * loss_2
                                epoch_loss[0] += loss_0; epoch_loss[1] += loss_1
                                epoch_loss[2] += loss_2
                            elif training_mode == 3:
                                pred, loss_0, loss_1 = self.model(target_data,
                                                [source_data], [source_labels])
                                loss = loss_0 + args.tradeoff[0] * loss_1
                                epoch_loss[0] += loss_0; epoch_loss[1] += loss_1
                            
                            # backward
                            loss.backward()
                            self.optimizer.step()
                    else:
                        with torch.no_grad():
                            if training_mode == 3:
                                pred = self.model(target_data, [source_data], [source_labels])
                            else:
                                pred = self.model(target_data)
                    epoch_acc += utils.get_accuracy(pred, target_labels)
                
                # Print the train and val information via each epoch
                epoch_acc = epoch_acc/num_iter
                if phase == 'train':
                    for key in epoch_loss.keys():
                        logging.info('{}-Loss_{}: {:.4f}'.format(phase, key,
                                                     epoch_loss[key]/num_iter))
                    logging.info('{}-Acc: {:.4f}'.format(phase, epoch_acc))
                else:
                    logging.info('{}-Acc: {:.4f}'.format(phase, epoch_acc))
                    
                    # log the best model according to the val accuracy
                    if epoch_acc > best_acc:
                        best_acc = epoch_acc
                        best_epoch = epoch
                    logging.info("The best model epoch {}, val-acc {:.4f}".format(best_epoch,
                                                                                  best_acc))
            if self.lr_scheduler is not None:
                    self.lr_scheduler.step()
    
    def train_mstln(self):
        args = self.args
        self._init_data()
    
        model = MSTLN.MSTLN(1, args.num_classes, self.num_source).to(self.device)
        D = nn.ModuleList([MSTLN.Discriminator(256).to(self.device)
                                               for _ in range(self.num_source)])
    
        optimizer = self._get_optimizer(model)
        optimizerD = self._get_optimizer(D)
        self.lr_scheduler = self._get_lr_scheduler(optimizer)
        
        best_acc = 0.0
        best_epoch = 0
        
        for epoch in range((args.max_epoch+1)):
            logging.info('-'*5 + 'Epoch {}/{}'.format(epoch, args.max_epoch) + '-'*5)
            
            if self.lr_scheduler is not None:
                logging.info('current lr: {}'.format(self.lr_scheduler.get_last_lr()))
                       
            model.train()
            D.train()
            
            correct, total = defaultdict(int), defaultdict(int)
            num_iter = len(self.iters['train'])
            for i in tqdm(range(3), ascii=True):
                tgt_inputs, tgt_labels = utils.get_next_batch(self.dataloaders, self.iters,
                                                              'train', self.device)
                tgt_feat_list = []
                loss_k, loss_shared, loss_da, loss_da_ms = 0.0, 0.0, 0.0, 0.0
                for idx, src in enumerate(args.source_name):
                    optimizer.zero_grad()
                    optimizerD.zero_grad()
        
                    src_inputs, src_labels = utils.get_next_batch(self.dataloaders, self.iters,
                                                                  src, self.device)
        
                    src_feat, src_pred = model(src_inputs, idx)
                    tgt_feat, tgt_pred = model(tgt_inputs, idx)
                    tgt_feat_list.append(tgt_feat)
                    
                    _, pred = torch.max(tgt_pred, -1)
                    correct[src] += (pred == tgt_labels).sum().item()
                    total[src] += tgt_labels.shape[0]
        
                    loss_k += F.nll_loss(torch.log(src_pred), src_labels)
        
                    unknown_mean = tgt_pred[:, -1].sum() / args.batch_size
                    loss_unknown = torch.Tensor([0.0])
                    for i in range(args.batch_size):
                        if tgt_pred[i, -1] >= unknown_mean:
                            loss_unknown -= torch.log(tgt_pred[i, -1])
                    loss_unknown /= args.batch_size
                    loss_unknown.backward()
                    optimizer.step()
        
                    sums = (1 - tgt_pred[:, -1]).sum()
                    for i in range(args.batch_size):
                        loss_shared -= (1 - tgt_pred[i, -1]) / sums * torch.mm(
                            tgt_pred[i, :-1].view(1, -1), torch.log(tgt_pred[i, :-1].view(-1, 1)))
        
                    src_val, tgt_val = D[idx](src_feat), D[idx](tgt_feat)
                    gradient_penalty = MSTLN.compute_gradient_penalty(D[idx],
                                                                 src_feat, tgt_feat)
                    loss_adv = -torch.sum(src_val) + torch.sum(tgt_val) + 10*gradient_penalty
                    loss_adv.backward()
                    optimizerD.step()
        
                    tgt_val = F.softmax(tgt_val, dim=1)
                    means = torch.mean((1 - tgt_pred[:, -1]).view(-1, 1) * tgt_val)
                    cb_factors = (1 - tgt_pred[:, -1]).view(-1, 1) * tgt_val / means
                    loss_da += utils.MFSAN_mmd(src_feat, tgt_feat, cb=cb_factors)

                for i in range(self.num_source - 1):
                    for j in range(i+1, self.num_source):
                        loss_da_ms += utils.MFSAN_mmd(tgt_feat_list[i], tgt_feat_list[j])
                
                loss_tl = loss_k + loss_da + loss_shared + loss_da_ms
                loss_tl.backward()
                optimizer.step()
            
            logging.info('Ending epoch {}'.format(epoch))
            logging.info('Training accuracy:')
            logging.info('\t'.join(args.source_name))
            logging.info('\t'.join(['%.03f' % (100.0*correct[d]/total[d]) for d in args.source_name]))
            
            epoch_acc = MSTLN.evaluate_acc(self.dataloaders, model, self.device)
            if epoch_acc > best_acc:
                best_acc = epoch_acc
                best_epoch = epoch
                logging.info("The best model epoch {}, val-acc {:.4f}".format(best_epoch,
                                                                              best_acc))
            if self.lr_scheduler is not None:
                self.lr_scheduler.step()
    
    def train_man_moe(self):
        args = self.args
        self._init_data()
        args.all_src = args.source_name + ['train']
        
        F_s = Man_Moe.FeatureExtractor(input_size=1, output_size=64, dropout=args.dropout)
        
        F_p = nn.Sequential(
            Man_Moe.FeatureExtractor(input_size=1, output_size=64, dropout=args.dropout),
            Man_Moe.MixtureOfExperts(input_size=64, num_source=self.num_source,
                                     output_size=64, dropout=args.dropout))
        
        C = Man_Moe.MoE_CNN(input_size=128, num_source=self.num_source,
                            output_size=args.num_classes, mode=2)
        
        D = Man_Moe.Discriminator(input_size=64, num_source=len(args.all_src))
        
        F_s, F_p, C, D = F_s.to(self.device), F_p.to(self.device), \
                         C.to(self.device), D.to(self.device)
                         
        # optimizers
        optimizer = self._get_optimizer([F_s, F_p, C])
        optimizerD = self._get_optimizer(D)
        
        # training
        best_acc = 0.0
        best_epoch = 0
        
        num_iter = int(utils.gmean([len(self.dataloaders[x]) for x in args.all_src]))
        for epoch in range((args.max_epoch+1)):
            F_s.train()
            F_p.train()
            C.train()
            D.train()
            
            # training accuracy
            correct, total = defaultdict(int), defaultdict(int)
            gate_correct, c_gate_correct = defaultdict(int), defaultdict(int)
            d_correct, d_total = 0, 0
            
            for i in tqdm(range(num_iter), ascii=True):
                
                # D iterations
                utils.freeze_net(F_s)
                utils.freeze_net(F_p)
                utils.freeze_net(C)
                utils.unfreeze_net(D)
                
                D.zero_grad()
                # train on both labeled and unlabeled source
                for src in args.all_src:
                    # targets not used
                    d_inputs, _ = utils.get_next_batch(self.dataloaders,
                                                       self.iters, src, self.device)
                    idx = args.all_src.index(src)
                    
                    shared_feat = F_s(d_inputs)
                    d_outputs = D(shared_feat)
                    
                    # if token-level D, we can reuse the gate label generator
                    d_targets = utils.get_gate_label(d_outputs, idx, self.device)
                    
                    # D accuracy
                    _, pred = torch.max(d_outputs, -1)
                    d_correct += (pred == d_targets).sum().item()
                    d_total += pred.shape[0]
                    l_d = F.nll_loss(d_outputs, d_targets)
                    l_d.backward()
                optimizerD.step()
                    
                # F&C iteration
                utils.unfreeze_net(F_s)
                utils.unfreeze_net(F_p)
                utils.unfreeze_net(C)
                utils.freeze_net(D)
                
                F_s.zero_grad()
                F_p.zero_grad()
                C.zero_grad()
                for src in args.source_name:
                    inputs, targets = utils.get_next_batch(self.dataloaders,
                                                           self.iters, src, self.device)
                    idx = args.all_src.index(src)
                    
                    shared_feat = F_s(inputs)
                    private_feat, gate_outputs = F_p(inputs)
                    c_outputs, c_gate_outputs = C((shared_feat, private_feat))
                    
                    # token-level gate loss
                    gate_targets = utils.get_gate_label(gate_outputs, idx, self.device)
                    loss_gate = F.cross_entropy(gate_outputs, gate_targets)
                    
                    _, gate_pred = torch.max(gate_outputs, -1)
                    gate_correct[src] += (gate_pred == gate_targets).sum().item()
                    
                    c_gate_targets = utils.get_gate_label(c_gate_outputs, idx, self.device)
                    loss_c_gate = F.cross_entropy(c_gate_outputs, c_gate_targets)

                    _, c_gate_pred = torch.max(c_gate_outputs, -1)
                    c_gate_correct[src] += (c_gate_pred == c_gate_targets).sum().item()
                   
                    loss_clf = F.nll_loss(c_outputs, targets)
                    _, pred = torch.max(c_outputs, -1)
                    correct[src] += (pred == targets).sum().item()
                    total[src] += pred.shape[0]
                    
                    loss = loss_clf + args.tradeoff[0] * loss_gate + args.tradeoff[1] * loss_c_gate
                    loss.backward()

                # update F with D gradients on all source
                for src in args.all_src:
                    inputs, _ = utils.get_next_batch(self.dataloaders,
                                                     self.iters, src, self.device)
                    idx = args.all_src.index(src)
                    shared_feat = F_s(inputs)
                    d_outputs = D(shared_feat)
                    
                    # if token-level D, we can reuse the gate label generator
                    d_targets = utils.get_gate_label(d_outputs, idx, self.device)
                    l_d = F.nll_loss(d_outputs, d_targets)
                    l_d *= -args.tradeoff[2]
                    l_d.backward()
                optimizer.step()

            # end of epoch
            logging.info('Ending epoch {}'.format(epoch))
            logging.info('D Training Accuracy: {:.3f}'.format(100.0*d_correct/d_total))
            logging.info('Training accuracy:')
            logging.info('\t'.join(args.source_name))
            logging.info('\t'.join(['%.03f' % (100.0*correct[d]/total[d])
                                                            for d in args.source_name]))
            logging.info('Gate accuracy:')
            logging.info('\t'.join(['%.03f' % (100.0*gate_correct[d]/total[d])
                                                            for d in args.source_name]))
            logging.info('Tagger Gate accuracy:')
            logging.info('\t'.join(['%.03f' % (100.0*c_gate_correct[d]/total[d])
                                                            for d in args.source_name]))   
            logging.info('Evaluating validation sets:')
            acc = Man_Moe.evaluate_acc(self.dataloaders, F_s, F_p, C, self.device)
            logging.info('Average validation accuracy: {:.3f}'.format(100.0*acc))

            if acc > best_acc:
                best_acc = acc
                best_epoch = epoch
            logging.info('Best epoch {} accuracy: {:.3f}'.format(best_epoch, 100.0*best_acc))

        # end of training
        logging.info('Best average validation accuracy: {:.3f}'.format(100.0*best_acc))
    