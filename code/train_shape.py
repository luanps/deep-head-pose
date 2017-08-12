import numpy as np
import torch
import torch.nn as nn
from torch.autograd import Variable
from torch.utils.data import DataLoader
from torchvision import transforms
import torchvision
import torch.backends.cudnn as cudnn
import torch.nn.functional as F

import cv2
import matplotlib.pyplot as plt
import sys
import os
import argparse

import datasets
import hopenet
import torch.utils.model_zoo as model_zoo

model_urls = {
    'resnet18': 'https://download.pytorch.org/models/resnet18-5c106cde.pth',
    'resnet34': 'https://download.pytorch.org/models/resnet34-333f7ec4.pth',
    'resnet50': 'https://download.pytorch.org/models/resnet50-19c8e357.pth',
    'resnet101': 'https://download.pytorch.org/models/resnet101-5d3b4d8f.pth',
    'resnet152': 'https://download.pytorch.org/models/resnet152-b121ed2d.pth',
}

def parse_args():
    """Parse input arguments."""
    parser = argparse.ArgumentParser(description='Head pose estimation using the Hopenet network.')
    parser.add_argument('--gpu', dest='gpu_id', help='GPU device id to use [0]',
            default=0, type=int)
    parser.add_argument('--num_epochs', dest='num_epochs', help='Maximum number of training epochs.',
          default=5, type=int)
    parser.add_argument('--batch_size', dest='batch_size', help='Batch size.',
          default=16, type=int)
    parser.add_argument('--lr', dest='lr', help='Base learning rate.',
          default=1e-5, type=float)
    parser.add_argument('--data_dir', dest='data_dir', help='Directory path for data.',
          default='', type=str)
    parser.add_argument('--filename_list', dest='filename_list', help='Path to text file containing relative paths for every example.',
          default='', type=str)

    args = parser.parse_args()

    return args

def get_ignored_params(model):
    # Generator function that yields ignored params.
    b = []
    b.append(model.conv1)
    b.append(model.bn1)
    b.append(model.layer1)
    b.append(model.layer2)
    b.append(model.layer3)
    b.append(model.layer4)
    for i in range(len(b)):
        for j in b[i].modules():
            for k in j.parameters():
                yield k

def get_non_ignored_params(model):
    # Generator function that yields params that will be optimized.
    b = []
    b.append(model.fc_yaw)
    b.append(model.fc_pitch)
    b.append(model.fc_roll)
    b.append(model.fc_shape_0)
    b.append(model.fc_shape_1)
    b.append(model.fc_shape_2)
    b.append(model.fc_shape_3)
    b.append(model.fc_shape_4)
    b.append(model.fc_shape_5)
    b.append(model.fc_shape_6)
    b.append(model.fc_shape_7)
    b.append(model.fc_shape_8)
    b.append(model.fc_shape_9)

    for i in range(len(b)):
        for j in b[i].modules():
            for k in j.parameters():
                    yield k

def load_filtered_state_dict(model, snapshot):
    # By user apaszke from discuss.pytorch.org
    model_dict = model.state_dict()
    # 1. filter out unnecessary keys
    snapshot = {k: v for k, v in snapshot.items() if k in model_dict}
    # 2. overwrite entries in the existing state dict
    model_dict.update(snapshot)
    # 3. load the new state dict
    model.load_state_dict(model_dict)

if __name__ == '__main__':
    args = parse_args()

    cudnn.enabled = True
    num_epochs = args.num_epochs
    batch_size = args.batch_size
    gpu = args.gpu_id

    if not os.path.exists('output/snapshots'):
        os.makedirs('output/snapshots')

    # ResNet101 with 3 outputs
    # model = hopenet.Hopenet(torchvision.models.resnet.Bottleneck, [3, 4, 23, 3], 66)
    # ResNet50
    model = hopenet.Hopenet_shape(torchvision.models.resnet.Bottleneck, [3, 4, 6, 3], 66, 60)
    # ResNet18
    # model = hopenet.Hopenet(torchvision.models.resnet.BasicBlock, [2, 2, 2, 2], 66)
    load_filtered_state_dict(model, model_zoo.load_url(model_urls['resnet50']))

    print 'Loading data.'

    transformations = transforms.Compose([transforms.Scale(224),transforms.RandomCrop(224),
                                          transforms.ToTensor()])

    pose_dataset = datasets.300W_LP(args.data_dir, args.filename_list,
                                transformations)
    train_loader = torch.utils.data.DataLoader(dataset=pose_dataset,
                                               batch_size=batch_size,
                                               shuffle=True,
                                               num_workers=2)

    model.cuda(gpu)
    criterion = nn.CrossEntropyLoss().cuda(gpu)
    reg_criterion = nn.MSELoss().cuda(gpu)
    # Regression loss coefficient
    alpha = 0.1

    idx_tensor = [idx for idx in xrange(66)]
    idx_tensor = torch.FloatTensor(idx_tensor).cuda(gpu)

    optimizer = torch.optim.Adam([{'params': get_ignored_params(model), 'lr': args.lr},
                                  {'params': get_non_ignored_params(model), 'lr': args.lr * 10}],
                                  lr = args.lr)

    print 'Ready to train network.'

    for epoch in range(num_epochs):
        for i, (images, labels, name) in enumerate(train_loader):
            images = Variable(images.cuda(gpu))
            label_yaw = Variable(labels[:,0].cuda(gpu))
            label_pitch = Variable(labels[:,1].cuda(gpu))
            label_roll = Variable(labels[:,2].cuda(gpu))
            label_shape = Variable(labels[:,3:].cuda(gpu))

            optimizer.zero_grad()
            model.zero_grad()

            yaw, pitch, roll, shape = model(images)

            # Cross entropy loss
            loss_yaw = criterion(yaw, label_yaw)
            loss_pitch = criterion(pitch, label_pitch)
            loss_roll = criterion(roll, label_roll)

            # MSE loss
            yaw_predicted = F.softmax(yaw)
            pitch_predicted = F.softmax(pitch)
            roll_predicted = F.softmax(roll)

            yaw_predicted = torch.sum(yaw_predicted.data * idx_tensor, 1)
            pitch_predicted = torch.sum(pitch_predicted.data * idx_tensor, 1)
            roll_predicted = torch.sum(roll_predicted.data * idx_tensor, 1)

            loss_reg_yaw = reg_criterion(yaw_predicted, label_yaw.float())
            loss_reg_pitch = reg_criterion(pitch_predicted, label_pitch.float())
            loss_reg_roll = reg_criterion(roll_predicted, label_roll.float())

            # Total loss
            loss_yaw += alpha * loss_reg_yaw
            loss_pitch += alpha * loss_reg_pitch
            loss_roll += alpha * loss_reg_roll

            loss_seq = [loss_yaw, loss_pitch, loss_roll]

            # Shape space loss
            for idx in xrange(len(shape)):
                loss_seq.append(criterion(shape[idx], label_shape[:,idx]))

            grad_seq = [torch.Tensor(1).cuda(gpu) for _ in range(len(loss_seq))]
            torch.autograd.backward(loss_seq, grad_seq)
            optimizer.step()

            # print ('Epoch [%d/%d], Iter [%d/%d] Losses: Yaw %.4f, Pitch %.4f, Roll %.4f'
            #        %(epoch+1, num_epochs, i+1, len(pose_dataset)//batch_size, loss_yaw.data[0], loss_pitch.data[0], loss_roll.data[0]))

            if (i+1) % 100 == 0:
                print ('Epoch [%d/%d], Iter [%d/%d] Losses: Yaw %.4f, Pitch %.4f, Roll %.4f, Shape %.4f'
                       %(epoch+1, num_epochs, i+1, len(pose_dataset)//batch_size, loss_yaw.data[0], loss_pitch.data[0], loss_roll.data[0], loss_seq[3].data[0]))
                if epoch == 0:
                    torch.save(model.state_dict(),
                    'output/snapshots/resnet50_shape_iter_'+ str(i+1) + '.pkl')

        # Save models at numbered epochs.
        if epoch % 1 == 0 and epoch < num_epochs - 1:
            print 'Taking snapshot...'
            torch.save(model.state_dict(),
            'output/snapshots/resnet50_shape_epoch_'+ str(epoch+1) + '.pkl')

    # Save the final Trained Model
    torch.save(model.state_dict(), 'output/snapshots/resnet50_shape_epoch_' + str(epoch+1) + '.pkl')
