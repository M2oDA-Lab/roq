# TODO: Make sampling test from long running optional

import os
import pickle
import math
from itertools import compress
import numpy as np
import pandas as pd
import torch
from torch_geometric.data import Data
from sklearn.model_selection import train_test_split
from torch_geometric.data import InMemoryDataset
from util.tcnn_util import prepare_trees, transformer, left_child, right_child
from IPython.display import clear_output


class queryPlanPGDataset(InMemoryDataset):
    def __init__(self, root='./',  split: str = "train", 
                 transform=None, pre_transform=None, 
                 pre_filter=None, force_reload = False,files_id=None, labeled_data_dir='./labeled_data/',
                 seed = 0, num_samples = None, val_samples = 0.1, test_samples = 0.01, test_longrun_share=0.5):
        self.files_id = files_id
        self.labeled_data_dir = labeled_data_dir
        self.seed = seed
        self.num_samples = num_samples
        self.val_samples = val_samples
        self.test_samples = test_samples
        self.test_longrun_share = test_longrun_share

        super().__init__(root, transform, pre_transform, pre_filter,force_reload=force_reload)

        if split == 'train':
            path = self.processed_paths[0]
        elif split == 'val':
            path = self.processed_paths[1]
        elif split == 'test':
            path = self.processed_paths[2]
        else:
            raise ValueError(f"Split '{split}' found, but expected either "
                             f"'train', 'val', or 'test'")

        self.data, self.slices = torch.load(path)
            
    @property
    def raw_file_names(self):
        return [os.path.join(self.labeled_data_dir,'labeled_query_plans_{}.pickle'.format(self.files_id))
               ]

    @property
    def processed_file_names(self):
        return [
            'proc_data_{}_tr.pt'.format(self.files_id),
            'proc_data_{}_val.pt'.format(self.files_id),
            'proc_data_{}_ts.pt'.format(self.files_id),
               ]

    def process(self):
        # Read data into huge `Data` list.
        with open(self.raw_file_names[0],'rb') as f:
            queries_list = pickle.load(f)
        
        indices = [i for i in range(len(queries_list))]
        
        if self.num_samples is not None:
            np.random.seed(self.seed)
            sample = np.random.choice(indices, size = self.num_samples, replace = False)
            queries_list=queries_list[sample]
        fin_samples = len(queries_list)

        # this loop prepares the plan trees
        plan_trees = []
        for query in queries_list:
            for j in query.plans.keys():
                plan = query.plans[j]
                plan_trees.append(plan.plan_tree)

        # prepare plan trees for TCNN
        prep_trees=prepare_trees(plan_trees, transformer, left_child, right_child, cuda=False)
        
        # create individual Data objects for each single sample
        # then put them in data_list
        data_list = []
        query_ids = []
        opt_plan_msk = []
        plan_latency_list = []
        prep_tree_id = 0
        for i,query in enumerate(queries_list):
            if i%5 == 0:
                clear_output(wait=True)
                print('Loading the data ... {}%'.format(math.floor((i/fin_samples)*100)))
            for j in query.plans.keys():
                plan = query.plans[j]
                
                # skip the plan if the plan's cost or latency are not populated
                if plan.latency is None or plan.cost.size == 0:
                    break
                
                # replace the latency for timed out plans with 5*opt_plan_latency 
                opt_plan = False
                plan_latency = plan.latency
                if plan.hintset_id == 0:
                    opt_plan = True
                    opt_plan_latency = plan.latency
                else:
                    if plan.timed_out == True:
                        plan_latency = 5*opt_plan_latency
                    
                opt_plan_msk.append(opt_plan)
                plan_latency_list.append(plan_latency)

                # assign plan tree attributes and orders
                prep_tree_attr = prep_trees[0][prep_tree_id]
                prep_tree_ord = prep_trees[1][prep_tree_id]
                prep_tree_id+=1

                data = Data(
                    x_s=torch.Tensor(query.node_attr),
                    edge_index_s=torch.Tensor(query.edge_indc),
                    edge_attr_s =torch.Tensor(query.edge_attr),
                    graph_attr = torch.Tensor(query.graph_attr),
                    y = torch.Tensor([float(plan_latency)]),
                    plan_attr=torch.Tensor(prep_tree_attr),
                    plan_ord=torch.Tensor(prep_tree_ord),
                    query_id = query.q_id,
                    num_joins = torch.Tensor([int(query.edge_indc.shape[1]/2)]),
                    opt_choice = torch.Tensor([opt_plan]),
                    opt_cost = torch.Tensor([float(plan.cost)]),
                    y_t = torch.Tensor([float(plan_latency)]),  # placeholder for transformed targets
                    num_nodes = torch.Tensor(query.node_attr).shape[0], 
                    # purturbed_runtimes = torch.Tensor(purturbed_runtimes[i]),
                    )
                query_ids.append(query.q_id)
                data_list.append(data)
        
        clear_output(wait=True)
        print('Loading the data ... {}%'.format(str(100)))
        
        if self.pre_filter is not None:
            data_list = [data for data in data_list if self.pre_filter(data)]

        if self.pre_transform is not None:
            data_list = [self.pre_transform(data) for data in data_list]
        
        # convert lists to arrays
        plan_latency_list = np.array(plan_latency_list)
        opt_plan_msk = np.array(opt_plan_msk)
        query_ids = np.array(query_ids)

        # get unique query ids
        query_ids_unique = pd.unique(query_ids)

        # transform ratio sample sizes to numbers
        if self.test_samples < 1:
            self.test_samples = int(self.test_samples*query_ids_unique.shape[0])
        if self.val_samples < 1:
            self.val_samples = int(self.val_samples*query_ids_unique.shape[0])

        # get query ids for long running queries
        opt_plan_latency=plan_latency_list[opt_plan_msk]
        long_running_msk = (opt_plan_latency>np.percentile(opt_plan_latency,q=99))
        long_running_qids = query_ids_unique[long_running_msk]
        non_long_qids = query_ids_unique[~long_running_msk]

        # determine the share of test data from long-running queries
        if self.test_longrun_share is None:
            self.test_longrun_share = self.test_samples/(query_ids_unique.size)
        
        # sample part 1 from long running queries
        train_val_qid1, test_qid1 = train_test_split(
            long_running_qids,test_size=self.test_longrun_share,
            random_state=self.seed, shuffle=True
            ) # by default gets 1/2 of all long running queries

        test_samples2 = int(self.test_samples - test_qid1.shape[0])

        # sample part 2 from non-long running queries
        train_val_qid2, test_qid2 = train_test_split(
            non_long_qids,
            test_size=test_samples2,
            random_state=self.seed, shuffle=True
            )
        
        # get the unions for short/long running query ids
        test_qid = np.union1d(test_qid1,test_qid2)
        train_val_qid = np.union1d(train_val_qid1,train_val_qid2)

        # get val split query ids
        train_qid, val_qid=train_test_split(
            train_val_qid,
            test_size=self.val_samples,
            random_state=self.seed, shuffle=True
            )
        
        # get split slices
        train_slice = np.isin(query_ids, train_qid)
        val_slice = np.isin(query_ids, val_qid)
        test_slice = np.isin(query_ids, test_qid)
        
        tr_data, tr_slices = self.collate(list(compress(data_list, train_slice)))
        torch.save((tr_data, tr_slices), self.processed_paths[0])
        
        val_data, val_slices = self.collate(list(compress(data_list, val_slice)))
        torch.save((val_data, val_slices), self.processed_paths[1])
        
        ts_data, ts_slices = self.collate(list(compress(data_list, test_slice)))

        torch.save((ts_data, ts_slices), self.processed_paths[2])


class queryPlanPGDataset_nosplit(InMemoryDataset):
    def __init__(self, root='./', 
                 transform=None, pre_transform=None, 
                 pre_filter=None, force_reload = False,files_id=None, labeled_data_dir='./labeled_data/',
                 seed = 0, num_samples = None):
        self.files_id = files_id
        self.labeled_data_dir = labeled_data_dir
        self.seed = seed
        self.num_samples = num_samples

        super().__init__(root, transform, pre_transform, pre_filter,force_reload=force_reload)

        path = self.processed_paths[0]
        self.data, self.slices = torch.load(path)
            
    @property
    def raw_file_names(self):
        return [os.path.join(self.labeled_data_dir,'labeled_query_plans_{}.pickle'.format(self.files_id))
               ]

    @property
    def processed_file_names(self):
        return [
            'proc_data_{}.pt'.format(self.files_id),
               ]

    def process(self):
        # Read data into huge `Data` list.
        with open(self.raw_file_names[0],'rb') as f:
            queries_list = pickle.load(f)
        
        indices = [i for i in range(len(queries_list))]
        
        if self.num_samples is not None:
            np.random.seed(self.seed)
            sample = np.random.choice(indices, size = self.num_samples, replace = False)
            queries_list=queries_list[sample]
        fin_samples = len(queries_list)

        # this loop prepares the plan trees
        plan_trees = []
        for query in queries_list:
            for j in query.plans.keys():
                plan = query.plans[j]
                plan_trees.append(plan.plan_tree)

        # prepare plan trees for TCNN
        prep_trees=prepare_trees(plan_trees, transformer, left_child, right_child, cuda=False)
        
        # create individual Data objects for each single sample
        # then put them in data_list
        data_list = []
        query_ids = []
        opt_plan_msk = []
        plan_latency_list = []
        prep_tree_id = 0
        for i,query in enumerate(queries_list):
            if i%5 == 0:
                clear_output(wait=True)
                print('Loading the data ... {}%'.format(math.floor((i/fin_samples)*100)))
            for j in query.plans.keys():
                plan = query.plans[j]
                
                # skip the plan if the plan's cost or latency are not populated
                if plan.latency is None or plan.cost.size == 0:
                    break
                
                # replace the latency for timed out plans with 5*opt_plan_latency 
                opt_plan = False
                plan_latency = plan.latency
                if plan.hintset_id == 0:
                    opt_plan = True
                    opt_plan_latency = plan.latency
                else:
                    if plan.timed_out == True:
                        plan_latency = 5*opt_plan_latency
                    
                opt_plan_msk.append(opt_plan)
                plan_latency_list.append(plan_latency)

                # assign plan tree attributes and orders
                prep_tree_attr = prep_trees[0][prep_tree_id]
                prep_tree_ord = prep_trees[1][prep_tree_id]
                prep_tree_id+=1

                data = Data(
                    x_s=torch.Tensor(query.node_attr),
                    edge_index_s=torch.Tensor(query.edge_indc),
                    edge_attr_s =torch.Tensor(query.edge_attr),
                    graph_attr = torch.Tensor(query.graph_attr),
                    y = torch.Tensor([float(plan_latency)]),
                    plan_attr=torch.Tensor(prep_tree_attr),
                    plan_ord=torch.Tensor(prep_tree_ord),
                    query_id = query.q_id,
                    num_joins = torch.Tensor([int(query.edge_indc.shape[1]/2)]),
                    opt_choice = torch.Tensor([opt_plan]),
                    opt_cost = torch.Tensor([float(plan.cost)]),
                    y_t = torch.Tensor([float(plan_latency)]),  # placeholder for transformed targets
                    num_nodes = torch.Tensor(query.node_attr).shape[0], 
                    # purturbed_runtimes = torch.Tensor(purturbed_runtimes[i]),
                    )
                query_ids.append(query.q_id)
                data_list.append(data)
        
        clear_output(wait=True)
        print('Loading the data ... {}%'.format(str(100)))
        
        if self.pre_filter is not None:
            data_list = [data for data in data_list if self.pre_filter(data)]

        if self.pre_transform is not None:
            data_list = [self.pre_transform(data) for data in data_list]
        
        # convert lists to arrays
        plan_latency_list = np.array(plan_latency_list)
        opt_plan_msk = np.array(opt_plan_msk)
        query_ids = np.array(query_ids)

        # get unique query ids
        query_ids_unique = pd.unique(query_ids)
        
        # get split slices
        train_slice = np.isin(query_ids, query_ids_unique)
        
        tr_data, tr_slices = self.collate(list(compress(data_list, train_slice)))
        torch.save((tr_data, tr_slices), self.processed_paths[0])
