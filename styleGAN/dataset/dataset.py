import os
import sys
import glob
import numpy as np
import cv2
import random
import torch
from torch.utils.data import Dataset
import common.utils.transforms as tf

class FaceDataset(Dataset):

    def __init__(self, data_root, initial_size=[4,4], istrain=True):
        super(FaceDataset, self).__init__()
        root_paths = data_root
        self.image_paths = sorted(glob.glob(os.path.join(root_paths, '*.png'))) + sorted(glob.glob(os.path.join(root_paths, '*.jpg')))
        self.imsize = initial_size

    def __len__(self):
        return len(self.image_paths)

    def setsize(self, image_size):
        self.imsize = image_size

    def shuffle(self):
        random.shuffle(self.image_paths)

    def __getitem__(self, idx):
        image = None
        while image is None:
            image = cv2.imread(self.image_paths[idx])
            if image is not None:
                if image.shape[0] > self.imsize[0] and image.shape[1] > self.imsize[1]:
                    break
                else:
                    image = None
            idx = np.random.randint(len(self.image_paths))
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        image = image.astype(np.float32)

        w, h, _ = image.shape

        # flip
        image = tf.random_flip(image)

        # rotation
        #low, high = [-10, 10]  
        #degree = np.random.randint(low, high)
        #image = tf.rotation(image, degree)

        # crop
        csize = int(w * 0.9) if w < h else int(h * 0.9)
        image = tf.random_crop(image, (csize, csize))

        # resize
        image = tf.rescale(image, (self.imsize[0], self.imsize[1]))

        # normalize
        image = image - 255 * 0.5
        image = image / (255 * 0.5)
  
        image = torch.from_numpy(image).permute(2,0,1).contiguous().float()
        return image


class MultiClassFaceDataset(Dataset):
    def __init__(self, cfg, istrain=True):
        super(MultiClassFaceDataset, self).__init__()
        self.cfg = cfg
        self.imsize = cfg.train.target_size
        root_path_list = cfg.train.dataset_list


        if os.path.isfile(root_path_list):
        ## danbooru face dataset
            with open(root_path_list, 'r') as f:
                line = f.readline().strip()
                self.tag_list = line.split(',')
                n_classes = len(self.tag_list)
                self.image_path_list = [[] for t in range(n_classes)]
                self.classes = [t for t in range(n_classes)]
                self.len_list = [0 for t in range(n_classes)]
                line = f.readline().strip()
                while(line):
                    image_path, cls = line.split(' ')
                    self.image_path_list[int(cls)].append(image_path)
                    self.len_list[int(cls)] += 1
                    line = f.readline().strip()
        ## million face dataset
        else:
            self.image_path_list, self.classes, self.len_list = [],[],[]
            for i in range(len(root_path_list)):
                image_paths = sorted(glob.glob(os.path.join(root_path_list[i], '*.png'))) + sorted(glob.glob(os.path.join(root_path_list[i], '*.jpg')))
                self.image_path_list.append(image_paths)
                self.classes.append(i)
                self.len_list.append(len(image_paths))

    def __len__(self):
        return sum(self.len_list)

    def __getitem__(self, idx, whichClass = None):
        if whichClass is None:
            whichClass = np.random.randint(len(self.classes))

        image_paths = self.image_path_list[whichClass]

        image = None
        while image is None:
            idx = np.random.randint(self.len_list[whichClass])
            image = cv2.imread(image_paths[idx])
            if image is not None:
                if image.shape[0] > 200 and image.shape[1] > 200:
                    break
                else:
                    image = None

        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        image = image.astype(np.float32)

        w, h, _ = image.shape

        # flip
        image = tf.random_flip(image)

        # rotation
        if hasattr(self.cfg.train.transform, 'rotation'):
            low, high = self.cfg.train.transform.rotation  
            degree = np.random.randint(low, high)
            image = tf.rotation(image, degree)

        # crop
        csize = int(w * 0.9) if iw < h else int(h * 0.9)
        image = tf.random_crop(image, (csize, csize))

        # resize
        image = tf.rescale(image, (self.imsize, self.imsize))

        # normalize
        image = image - 255 * 0.5
        image = image / (255 * 0.5)
  
        image = torch.from_numpy(image).permute(2,0,1).contiguous().float()
        return image, whichClass
