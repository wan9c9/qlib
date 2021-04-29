# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""
The Trainer will train a list of tasks and return a list of model recorder.
There are two steps in each Trainer including `train`(make model recorder) and `end_train`(modify model recorder).

This is concept called "DelayTrainer", which can be used in online simulating to parallel training.
In "DelayTrainer", the first step is only to save some necessary info to model recorder, and the second step which will be finished in the end can do some concurrent and time-consuming operations such as model fitting.

`Qlib` offer two kind of Trainer, TrainerR is simplest and TrainerRM is based on TaskManager to help manager tasks lifecycle automatically. 
"""

import socket
import time
from typing import Callable, List

from qlib.data.dataset import Dataset
from qlib.model.base import Model
from qlib.utils import flatten_dict, get_cls_kwargs, init_instance_by_config
from qlib.workflow import R
from qlib.workflow.record_temp import SignalRecord
from qlib.workflow.recorder import Recorder
from qlib.workflow.task.manage import TaskManager, run_task


def begin_task_train(task_config: dict, experiment_name: str, recorder_name: str = None) -> Recorder:
    """
    Begin a task training to start a recorder and save the task config.

    Args:
        task_config (dict): the config of a task
        experiment_name (str): the name of experiment
        recorder_name (str): the given name will be the recorder name. None for using rid.

    Returns:
        Recorder: the model recorder
    """
    # FIXME: recorder_id
    if recorder_name is None:
        recorder_name = str(time.time())
    with R.start(experiment_name=experiment_name, recorder_name=recorder_name):
        R.log_params(**flatten_dict(task_config))
        R.save_objects(**{"task": task_config})  # keep the original format and datatype
        R.set_tags(**{"hostname": socket.gethostname()})
        recorder: Recorder = R.get_recorder()
    return recorder


def end_task_train(rec: Recorder, experiment_name: str) -> Recorder:
    """
    Finish task training with real model fitting and saving.

    Args:
        rec (Recorder): the recorder will be resumed
        experiment_name (str): the name of experiment

    Returns:
        Recorder: the model recorder
    """
    with R.start(experiment_name=experiment_name, recorder_name=rec.info["name"], resume=True):
        task_config = R.load_object("task")
        # model & dataset initiation
        model: Model = init_instance_by_config(task_config["model"])
        dataset: Dataset = init_instance_by_config(task_config["dataset"])
        # model training
        model.fit(dataset)
        R.save_objects(**{"params.pkl": model})
        # this dataset is saved for online inference. So the concrete data should not be dumped
        dataset.config(dump_all=False, recursive=True)
        R.save_objects(**{"dataset": dataset})
        # generate records: prediction, backtest, and analysis
        records = task_config.get("record", [])
        if isinstance(records, dict):  # prevent only one dict
            records = [records]
        for record in records:
            cls, kwargs = get_cls_kwargs(record, default_module="qlib.workflow.record_temp")
            if cls is SignalRecord:
                rconf = {"model": model, "dataset": dataset, "recorder": rec}
            else:
                rconf = {"recorder": rec}
            r = cls(**kwargs, **rconf)
            r.generate()

    return rec


def task_train(task_config: dict, experiment_name: str) -> Recorder:
    """
    Task based training, will be divided into two steps.

    Parameters
    ----------
    task_config : dict
        The config of a task.
    experiment_name: str
        The name of experiment

    Returns
    ----------
    Recorder : The instance of the recorder
    """
    recorder = begin_task_train(task_config, experiment_name)
    recorder = end_task_train(recorder, experiment_name)
    return recorder


class Trainer:
    """
    The trainer which can train a list of model
    """

    def __init__(self):
        self.delay = False

    def train(self, tasks: list, *args, **kwargs) -> list:
        """
        Given a list of model definition, begin a training and return the models.

        Args:
            tasks: a list of tasks

        Returns:
            list: a list of models
        """
        raise NotImplementedError(f"Please implement the `train` method.")

    def end_train(self, models: list, *args, **kwargs) -> list:
        """
        Given a list of models, finished something in the end of training if you need.
        The models maybe Recorder, txt file, database and so on.

        Args:
            models: a list of models

        Returns:
            list: a list of models
        """
        # do nothing if you finished all work in `train` method
        return models

    def is_delay(self) -> bool:
        """
        If Trainer will delay finishing `end_train`.

        Returns:
            bool: if DelayTrainer
        """
        return self.delay

    def reset(self):
        """
        Reset the Trainer status.
        """
        pass


class TrainerR(Trainer):
    """
    Trainer based on (R)ecorder.
    It will train a list of tasks and return a list of model recorder in a linear way.

    Assumption: models were defined by `task` and the results will saved to `Recorder`
    """

    def __init__(self, experiment_name: str, train_func: Callable = task_train):
        """
        Init TrainerR.

        Args:
            experiment_name (str): the name of experiment.
            train_func (Callable, optional): default training method. Defaults to `task_train`.
        """
        super().__init__()
        self.experiment_name = experiment_name
        self.train_func = train_func

    def train(self, tasks: list, train_func: Callable = None, **kwargs) -> List[Recorder]:
        """
        Given a list of `task`s and return a list of trained Recorder. The order can be guaranteed.

        Args:
            tasks (list): a list of definition based on `task` dict
            train_func (Callable): the train method which need at least `task`s and `experiment_name`. None for default training method.
            kwargs: the params for train_func.

        Returns:
            list: a list of Recorders
        """
        if train_func is None:
            train_func = self.train_func
        recs = []
        for task in tasks:
            rec = train_func(task, self.experiment_name, **kwargs)
            rec.set_tags(**{"train_status": "begin_task_train"})
            recs.append(rec)
        return recs

    def end_train(self, recs: list, **kwargs) -> list:
        for rec in recs:
            rec.set_tags(**{"train_status": "end_task_train"})
        return recs


class DelayTrainerR(TrainerR):
    """
    A delayed implementation based on TrainerR, which means `train` method may only do some preparation and `end_train` method can do the real model fitting.
    """

    def __init__(self, experiment_name, train_func=begin_task_train, end_train_func=end_task_train):
        """
        Init TrainerRM.

        Args:
            experiment_name (str): the name of experiment.
            train_func (Callable, optional): default train method. Defaults to `begin_task_train`.
            end_train_func (Callable, optional): default end_train method. Defaults to `end_task_train`.
        """
        super().__init__(experiment_name, train_func)
        self.end_train_func = end_train_func
        self.delay = True

    def end_train(self, recs, end_train_func=None, **kwargs) -> List[Recorder]:
        """
        Given a list of Recorder and return a list of trained Recorder.
        This class will finish real data loading and model fitting.

        Args:
            recs (list): a list of Recorder, the tasks have been saved to them
            end_train_func (Callable, optional): the end_train method which need at least `recorder`s and `experiment_name`. Defaults to None for using self.end_train_func.
            kwargs: the params for end_train_func.

        Returns:
            list: a list of Recorders
        """
        if end_train_func is None:
            end_train_func = self.end_train_func
        for rec in recs:
            end_train_func(rec, **kwargs)
            rec.set_tags(**{"train_status": "end_task_train"})
        return recs


class TrainerRM(Trainer):
    """
    Trainer based on (R)ecorder and Task(M)anager.
    It can train a list of tasks and return a list of model recorder in a multiprocessing way.

    Assumption: `task` will be saved to TaskManager and `task` will be fetched and trained from TaskManager
    """

    def __init__(self, experiment_name: str, task_pool: str, train_func=task_train):
        """
        Init TrainerR.

        Args:
            experiment_name (str): the name of experiment.
            task_pool (str): task pool name in TaskManager.
            train_func (Callable, optional): default training method. Defaults to `task_train`.
        """
        super().__init__()
        self.experiment_name = experiment_name
        self.task_pool = task_pool
        self.train_func = train_func

    def train(
        self,
        tasks: list,
        train_func: Callable = None,
        before_status: str = TaskManager.STATUS_WAITING,
        after_status: str = TaskManager.STATUS_DONE,
        **kwargs,
    ) -> List[Recorder]:
        """
        Given a list of `task`s and return a list of trained Recorder. The order can be guaranteed.

        This method defaults to a single process, but TaskManager offered a great way to parallel training.
        Users can customize their train_func to realize multiple processes or even multiple machines.

        Args:
            tasks (list): a list of definition based on `task` dict
            train_func (Callable): the train method which need at least `task`s and `experiment_name`. None for default training method.
            before_status (str): the tasks in before_status will be fetched and trained. Can be STATUS_WAITING, STATUS_PART_DONE.
            after_status (str): the tasks after trained will become after_status. Can be STATUS_WAITING, STATUS_PART_DONE.
            kwargs: the params for train_func.

        Returns:
            list: a list of Recorders
        """
        if train_func is None:
            train_func = self.train_func
        tm = TaskManager(task_pool=self.task_pool)
        _id_list = tm.create_task(tasks)  # all tasks will be saved to MongoDB
        run_task(
            train_func,
            self.task_pool,
            experiment_name=self.experiment_name,
            before_status=before_status,
            after_status=after_status,
            **kwargs,
        )

        recs = []
        for _id in _id_list:
            rec = tm.re_query(_id)["res"]
            rec.set_tags(**{"train_status": "begin_task_train"})
            recs.append(rec)
        return recs

    def end_train(self, recs: list, **kwargs) -> list:
        for rec in recs:
            rec.set_tags(**{"train_status": "end_task_train"})
        return recs

    def reset(self):
        """
        NOTE: this method will delete all task in this task_pool!
        """
        tm = TaskManager(task_pool=self.task_pool)
        tm.remove()


class DelayTrainerRM(TrainerRM):
    """
    A delayed implementation based on TrainerRM, which means `train` method may only do some preparation and `end_train` method can do the real model fitting.

    """

    def __init__(self, experiment_name, task_pool: str, train_func=begin_task_train, end_train_func=end_task_train):
        super().__init__(experiment_name, task_pool, train_func)
        self.end_train_func = end_train_func
        self.delay = True

    def train(self, tasks: list, train_func=None, **kwargs):
        """
        Same as `train` of TrainerRM, after_status will be STATUS_PART_DONE.
        Args:
            tasks (list): a list of definition based on `task` dict
            train_func (Callable): the train method which need at least `task`s and `experiment_name`. Defaults to None for using self.train_func.
        Returns:
            list: a list of Recorders
        """
        return super().train(tasks, train_func=train_func, after_status=TaskManager.STATUS_PART_DONE, **kwargs)

    def end_train(self, recs, end_train_func=None, **kwargs):
        """
        Given a list of Recorder and return a list of trained Recorder.
        This class will finish real data loading and model fitting.

        Args:
            recs (list): a list of Recorder, the tasks have been saved to them.
            end_train_func (Callable, optional): the end_train method which need at least `recorder`s and `experiment_name`. Defaults to None for using self.end_train_func.
            kwargs: the params for end_train_func.

        Returns:
            list: a list of Recorders
        """

        if end_train_func is None:
            end_train_func = self.end_train_func
        run_task(
            end_train_func,
            self.task_pool,
            experiment_name=self.experiment_name,
            before_status=TaskManager.STATUS_PART_DONE,
            **kwargs,
        )
        for rec in recs:
            rec.set_tags(**{"train_status": "end_task_train"})
        return recs
