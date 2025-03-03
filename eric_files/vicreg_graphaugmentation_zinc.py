# -*- coding: utf-8 -*-
"""VicReg_GraphAugmentation_ZINC.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1KejcXUsj2BnT-pR-J7dX0xLH9FiXMPLY
"""

# https://arxiv.org/abs/1610.02415

# https://pytorch-geometric.readthedocs.io/en/latest/notes/introduction.html

import torch
print(torch.__version__)
import torch.nn.functional as F
import torch.nn as nn
import torch.distributed as dist

import torch_geometric
#print(torch_geometric.__version__)
from torch_geometric.datasets import ZINC
import GCL.augmentors
import GCL.augmentors as A

from sklearn.linear_model import RidgeClassifierCV, LogisticRegression

torch_geometric.datasets.ZINC

train_dataset = ZINC(root = 'data/', subset = 'true', split = 'train') # subset false -> 250k graphs
                                      # subset true -> 12k graphs
val_dataset = ZINC(root = 'data/', split = 'val')

parameters = {}
parameters['batch_size'] = 64

from torch_geometric.loader import DataLoader
import torch

infinity = int(1e9)

train_loader = DataLoader(train_dataset, batch_size=parameters['batch_size'], shuffle=True)

train_big_subset = DataLoader(train_dataset, batch_size = 4096, shuffle = True)
val_loader = DataLoader(val_dataset, batch_size = infinity, shuffle = False)

# Data Transforms
# Transforms are a common way in torchvision to transform images and perform augmentation. PyG comes with its own transforms,

from torch_geometric.nn import GCNConv

class GCN(torch.nn.Module):
    def __init__(self):
        super().__init__()
        
        self.rep_dim = 128
        #self.emb_dim = 64
        
        self.conv1 = GCNConv(train_dataset.num_node_features, self.rep_dim // 2)
        self.bn1 = nn.BatchNorm1d(self.rep_dim // 2)
        self.a1 = nn.LeakyReLU(0.02)
        
        self.conv2 = GCNConv(self.rep_dim // 2, self.rep_dim) # To Rep Space
        self.bn2 = nn.BatchNorm1d(self.rep_dim)
        self.a2 = nn.LeakyReLU(0.02)
        
        self.conv3 = GCNConv(self.rep_dim, self.rep_dim * 2) # To Emb Space
        self.bn3 = nn.BatchNorm1d(self.rep_dim * 2)
        
        self.fc1 = nn.Linear(self.rep_dim * 2, 999) # Linear to rep?
        
    def forward(self, data):
        x = data[0].float().to(device)
        edge_index = data[1].to(device)
        
        #print(x.dtype)
        #print(edge_index.dtype)
        #x, edge_index = data.x.float(), data.edge_index
        
        x = self.conv1(x, edge_index)
        x = self.a1(self.bn1(x))
        x = F.dropout(x, training=self.training)
        
        x = self.conv2(x, edge_index)
        #x = self.a2(self.bn2(x))
        #x = F.dropout(x, training=self.training)
        x_rep = self.bn2(x)
        x_emb = self.conv3(x_rep, edge_index)

        # Can have the -> rep and -> emb layers be linear layers on the graph conv output
        x_fc1 = self.fc1(x_emb)
        #print('from conv3 to linear output', x_fc1.shape)
        
        return x_rep, x_emb
    
    def pair_emb_rep(self, x1, x2):
        
        return self.forward(x1), self.forward(x2)
    
from sklearn.linear_model import LinearRegression

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
print(device)
model = GCN().to(device)
#data = train_dataset[0].to(device)
optimizer = torch.optim.Adam(model.parameters(), lr=0.01, weight_decay=5e-4)


aug = A.RandomChoice([#A.RWSampling(num_seeds=1000, walk_length=10),
                      A.NodeDropping(pn=0.1),
                      A.FeatureMasking(pf=0.1),
                      A.EdgeRemoving(pe=0.1)],
                     num_choices=1)

val_aug = A.RandomChoice([], num_choices = 0)


def barlow(batch):
    # Return two random views of input batch
    return aug(batch[0], batch[1]), aug(batch[0], batch[1])

def off_diagonal(x):
    n, m = x.shape
    assert n == m
    return x.flatten()[:-1].view(n - 1, n + 1)[:, 1:].flatten()

class FullGatherLayer(torch.autograd.Function):
    """
    Gather tensors from all process and support backward propagation
    for the gradients across processes.
    """

    @staticmethod
    def forward(ctx, x):
        output = [torch.zeros_like(x) for _ in range(dist.get_world_size())]
        dist.all_gather(output, x)
        return tuple(output)

    @staticmethod
    def backward(ctx, *grads):
        all_gradients = torch.stack(grads)
        dist.all_reduce(all_gradients)
        return all_gradients[dist.get_rank()]
    
def VicRegLoss(x, y):
    # https://github.com/facebookresearch/vicreg/blob/4e12602fd495af83efd1631fbe82523e6db092e0/main_vicreg.py#L184
    # x, y are output of projector(backbone(x and y))
    repr_loss = F.mse_loss(x, y)

    x = x - x.mean(dim=0)
    y = y - y.mean(dim=0)

    std_x = torch.sqrt(x.var(dim=0) + 0.0001)
    std_y = torch.sqrt(y.var(dim=0) + 0.0001)
    std_loss = torch.mean(F.relu(1 - std_x)) / 2 + torch.mean(F.relu(1 - std_y)) / 2

    cov_x = (x.T @ x) / (parameters['batch_size'] - 1)
    cov_y = (y.T @ y) / (parameters['batch_size'] - 1)
    cov_loss = off_diagonal(cov_x).pow_(2).sum().div(
        x.shape[1]
    ) + off_diagonal(cov_y).pow_(2).sum().div(x.shape[1])
    
    # self.num_features -> rep_dim?
    loss = (
        sim_coeff * repr_loss
        + std_coeff * std_loss
        + cov_coeff * cov_loss
    )
    return loss

sim_coeff = 25
std_coeff = 25
cov_coeff = 1

model.train()
for epoch in range(5):
    
    epo_losses = []
    for batch in train_loader:
        #batch = batch.to(device)
        batch.x = batch.x.float()#.to(device)
        #batch.edge_index = batch.edge_index.to(device)

        optimizer.zero_grad()
        
        # Barlow - get 2 random views of batch
        b1 = aug(batch.x, batch.edge_index, batch.edge_attr)
        b2 = aug(batch.x, batch.edge_index, batch.edge_attr)
        
                
        # Embed each batch (ignoring representations)
        [r1, e1], [r2, e2] = model.pair_emb_rep(b1, b2)

        # VicReg loss on projections
        loss = VicRegLoss(e1, e2)
        
        loss.backward()
        optimizer.step()
        
        epo_losses.append(loss.data.item())
        
    print(sum(epo_losses) / len(epo_losses))
    
    ############################
    ## Per-epoch validation step:


    # Embed Training Samples:
    train_batch = next(iter(train_big_subset))
    #print('train batch', train_batch)
    train_batch = val_aug(train_batch.x, train_batch.edge_index, train_batch.edge_attr) # val_aug is an empty augmentation
    #print('train_batch augd', train_batch)

    with torch.no_grad():
        tr_rep, _ = model.forward(train_batch)
    #print(tr_rep.shape)

    # Train linear model on embedded samples:
    ridge_mod = RidgeClassifierCV(cv = 4).fit(tr_rep, y_train)
    linear_mod = LogisticRegression(penalty = None).fit(tr_rep, y_train)

    # Embed validation samples:
    val_batch = next(iter(val_loader))
    #print('val batch', val_batch)
    val_batch = val_aug(val_batch.x, val_batch.edge_index, val_batch.edge_attr) # val_aug is an empty augmentation
    #print('val_batch augd', val_batch)
    
    with torch.no_grad():
        val_rep, _ = model.forward(val_batch)
    #print(val_rep.shape)

    # Test linear model on embedded samples:
    ridge_score = f1_score(ridge_mod.predict(val_rep), y_val)
    linear_score = f1_score(linear_mod.predict(val_rep), y_val)
    
    print(f'Classifier Scores at Epoch {epoch}:', round(linear_score, 3), round(ridge_score, 3))

if False: # Update for some downstream? Keep in mind this idea of graph masking
    # Evaluate
    model.eval()
    pred = model(data).argmax(dim=1)
    correct = (pred[data.test_mask] == data.y[data.test_mask]).sum()
    acc = int(correct) / int(data.test_mask.sum())
    print(f'Accuracy: {acc:.4f}')



