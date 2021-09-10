# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
from argparse import Namespace
from types import MethodType
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Type, Union

from torch.optim import Optimizer

from pytorch_lightning import Callback, LightningDataModule, LightningModule, seed_everything, Trainer
from pytorch_lightning.utilities import _JSONARGPARSE_AVAILABLE, rank_zero_warn, warnings
from pytorch_lightning.utilities.cloud_io import get_filesystem
from pytorch_lightning.utilities.exceptions import MisconfigurationException
from pytorch_lightning.utilities.model_helpers import is_overridden
from pytorch_lightning.utilities.types import LRSchedulerType, LRSchedulerTypeTuple

if _JSONARGPARSE_AVAILABLE:
    from jsonargparse import ActionConfigFile, ArgumentParser, class_from_function, set_config_read_mode
    from jsonargparse.actions import _ActionSubCommands
    from jsonargparse.optionals import import_docstring_parse

    set_config_read_mode(fsspec_enabled=True)
else:
    ArgumentParser = object


class LightningArgumentParser(ArgumentParser):
    """Extension of jsonargparse's ArgumentParser for pytorch-lightning."""

    def __init__(self, *args: Any, parse_as_dict: bool = True, **kwargs: Any) -> None:
        """Initialize argument parser that supports configuration file input.

        For full details of accepted arguments see `ArgumentParser.__init__
        <https://jsonargparse.readthedocs.io/en/stable/#jsonargparse.core.ArgumentParser.__init__>`_.
        """
        if not _JSONARGPARSE_AVAILABLE:
            raise ModuleNotFoundError(
                "`jsonargparse` is not installed but it is required for the CLI."
                " Install it with `pip install jsonargparse[signatures]`."
            )
        super().__init__(*args, parse_as_dict=parse_as_dict, **kwargs)
        self.add_argument(
            "--config", action=ActionConfigFile, help="Path to a configuration file in json or yaml format."
        )
        self.callback_keys: List[str] = []
        self.optimizers_and_lr_schedulers: Dict[str, Tuple[Union[Type, Tuple[Type, ...]], str]] = {}

    def add_lightning_class_args(
        self,
        lightning_class: Union[
            Callable[..., Union[Trainer, LightningModule, LightningDataModule, Callback]],
            Type[Trainer],
            Type[LightningModule],
            Type[LightningDataModule],
            Type[Callback],
        ],
        nested_key: str,
        subclass_mode: bool = False,
    ) -> List[str]:
        """Adds arguments from a lightning class to a nested key of the parser.

        Args:
            lightning_class: A callable or any subclass of {Trainer, LightningModule, LightningDataModule, Callback}.
            nested_key: Name of the nested namespace to store arguments.
            subclass_mode: Whether allow any subclass of the given class.

        Returns:
            A list with the names of the class arguments added.
        """
        if callable(lightning_class) and not isinstance(lightning_class, type):
            lightning_class = class_from_function(lightning_class)

        if isinstance(lightning_class, type) and issubclass(
            lightning_class, (Trainer, LightningModule, LightningDataModule, Callback)
        ):
            if issubclass(lightning_class, Callback):
                self.callback_keys.append(nested_key)
            if subclass_mode:
                return self.add_subclass_arguments(lightning_class, nested_key, required=True)
            return self.add_class_arguments(
                lightning_class, nested_key, fail_untyped=False, instantiate=not issubclass(lightning_class, Trainer)
            )
        raise MisconfigurationException(
            f"Cannot add arguments from: {lightning_class}. You should provide either a callable or a subclass of: "
            "Trainer, LightningModule, LightningDataModule, or Callback."
        )

    def add_optimizer_args(
        self,
        optimizer_class: Union[Type[Optimizer], Tuple[Type[Optimizer], ...]],
        nested_key: str = "optimizer",
        link_to: str = "AUTOMATIC",
    ) -> None:
        """Adds arguments from an optimizer class to a nested key of the parser.

        Args:
            optimizer_class: Any subclass of torch.optim.Optimizer.
            nested_key: Name of the nested namespace to store arguments.
            link_to: Dot notation of a parser key to set arguments or AUTOMATIC.
        """
        if isinstance(optimizer_class, tuple):
            assert all(issubclass(o, Optimizer) for o in optimizer_class)
        else:
            assert issubclass(optimizer_class, Optimizer)
        kwargs = {"instantiate": False, "fail_untyped": False, "skip": {"params"}}
        if isinstance(optimizer_class, tuple):
            self.add_subclass_arguments(optimizer_class, nested_key, required=True, **kwargs)
        else:
            self.add_class_arguments(optimizer_class, nested_key, **kwargs)
        self.optimizers_and_lr_schedulers[nested_key] = (optimizer_class, link_to)

    def add_lr_scheduler_args(
        self,
        lr_scheduler_class: Union[LRSchedulerType, Tuple[LRSchedulerType, ...]],
        nested_key: str = "lr_scheduler",
        link_to: str = "AUTOMATIC",
    ) -> None:
        """Adds arguments from a learning rate scheduler class to a nested key of the parser.

        Args:
            lr_scheduler_class: Any subclass of ``torch.optim.lr_scheduler.{_LRScheduler, ReduceLROnPlateau}``.
            nested_key: Name of the nested namespace to store arguments.
            link_to: Dot notation of a parser key to set arguments or AUTOMATIC.
        """
        if isinstance(lr_scheduler_class, tuple):
            assert all(issubclass(o, LRSchedulerTypeTuple) for o in lr_scheduler_class)
        else:
            assert issubclass(lr_scheduler_class, LRSchedulerTypeTuple)
        kwargs = {"instantiate": False, "fail_untyped": False, "skip": {"optimizer"}}
        if isinstance(lr_scheduler_class, tuple):
            self.add_subclass_arguments(lr_scheduler_class, nested_key, required=True, **kwargs)
        else:
            self.add_class_arguments(lr_scheduler_class, nested_key, **kwargs)
        self.optimizers_and_lr_schedulers[nested_key] = (lr_scheduler_class, link_to)


class SaveConfigCallback(Callback):
    """Saves a LightningCLI config to the log_dir when training starts.

    Raises:
        RuntimeError: If the config file already exists in the directory to avoid overwriting a previous run
    """

    def __init__(
        self,
        parser: LightningArgumentParser,
        config: Union[Namespace, Dict[str, Any]],
        config_filename: str,
        overwrite: bool = False,
    ) -> None:
        self.parser = parser
        self.config = config
        self.config_filename = config_filename
        self.overwrite = overwrite

    def setup(self, trainer: Trainer, pl_module: LightningModule, stage: Optional[str] = None) -> None:
        # save the config in `setup` because (1) we want it to save regardless of the trainer function run
        # and we want to save before processes are spawned
        log_dir = trainer.log_dir
        assert log_dir is not None
        config_path = os.path.join(log_dir, self.config_filename)
        if not self.overwrite and os.path.isfile(config_path):
            raise RuntimeError(
                f"{self.__class__.__name__} expected {config_path} to NOT exist. Aborting to avoid overwriting"
                " results of a previous run. You can delete the previous config file,"
                " set `LightningCLI(save_config_callback=None)` to disable config saving,"
                " or set `LightningCLI(save_config_overwrite=True)` to overwrite the config file."
            )
        if trainer.is_global_zero:
            # save only on rank zero to avoid race conditions on DDP.
            # the `log_dir` needs to be created as we rely on the logger to do it usually
            # but it hasn't logged anything at this point
            get_filesystem(log_dir).makedirs(log_dir, exist_ok=True)
            self.parser.save(self.config, config_path, skip_none=False, overwrite=self.overwrite)

    def __reduce__(self) -> Tuple[Type["SaveConfigCallback"], Tuple, Dict]:
        # `ArgumentParser` is un-pickleable. Drop it
        return self.__class__, (None, self.config, self.config_filename), {}


class LightningCLI:
    """Implementation of a configurable command line tool for pytorch-lightning."""

    def __init__(
        self,
        model_class: Union[Type[LightningModule], Callable[..., LightningModule]],
        datamodule_class: Optional[Union[Type[LightningDataModule], Callable[..., LightningDataModule]]] = None,
        save_config_callback: Optional[Type[SaveConfigCallback]] = SaveConfigCallback,
        save_config_filename: str = "config.yaml",
        save_config_overwrite: bool = False,
        trainer_class: Union[Type[Trainer], Callable[..., Trainer]] = Trainer,
        trainer_defaults: Optional[Dict[str, Any]] = None,
        seed_everything_default: Optional[int] = None,
        description: str = "pytorch-lightning trainer command line tool",
        env_prefix: str = "PL",
        env_parse: bool = False,
        parser_kwargs: Optional[Union[Dict[str, Any], Dict[str, Dict[str, Any]]]] = None,
        subclass_mode_model: bool = False,
        subclass_mode_data: bool = False,
        run: bool = True,
    ) -> None:
        """Receives as input pytorch-lightning classes (or callables which return pytorch-lightning classes), which
        are called / instantiated using a parsed configuration file and / or command line args.

        Parsing of configuration from environment variables can be enabled by setting ``env_parse=True``.
        A full configuration yaml would be parsed from ``PL_CONFIG`` if set.
        Individual settings are so parsed from variables named for example ``PL_TRAINER__MAX_EPOCHS``.

        For more info, read :ref:`the CLI docs <common/lightning_cli:LightningCLI>`.

        .. warning:: ``LightningCLI`` is in beta and subject to change.

        Args:
            model_class: :class:`~pytorch_lightning.core.lightning.LightningModule` class to train on or a callable
                which returns a :class:`~pytorch_lightning.core.lightning.LightningModule` instance when called.
            datamodule_class: An optional :class:`~pytorch_lightning.core.datamodule.LightningDataModule` class or a
                callable which returns a :class:`~pytorch_lightning.core.datamodule.LightningDataModule` instance when
                called.
            save_config_callback: A callback class to save the training config.
            save_config_filename: Filename for the config file.
            save_config_overwrite: Whether to overwrite an existing config file.
            trainer_class: An optional subclass of the :class:`~pytorch_lightning.trainer.trainer.Trainer` class or a
                callable which returns a :class:`~pytorch_lightning.trainer.trainer.Trainer` instance when called.
            trainer_defaults: Set to override Trainer defaults or add persistent callbacks.
            seed_everything_default: Default value for the :func:`~pytorch_lightning.utilities.seed.seed_everything`
                seed argument.
            description: Description of the tool shown when running ``--help``.
            env_prefix: Prefix for environment variables.
            env_parse: Whether environment variable parsing is enabled.
            parser_kwargs: Additional arguments to instantiate each ``LightningArgumentParser``.
            subclass_mode_model: Whether model can be any `subclass
                <https://jsonargparse.readthedocs.io/en/stable/#class-type-and-sub-classes>`_
                of the given class.
            subclass_mode_data: Whether datamodule can be any `subclass
                <https://jsonargparse.readthedocs.io/en/stable/#class-type-and-sub-classes>`_
                of the given class.
            run: Whether subcommands should be added to run a :class:`~pytorch_lightning.trainer.trainer.Trainer`
                method. If set to ``False``, the trainer and model classes will be instantiated only.
        """
        self.model_class = model_class
        self.datamodule_class = datamodule_class
        self.save_config_callback = save_config_callback
        self.save_config_filename = save_config_filename
        self.save_config_overwrite = save_config_overwrite
        self.trainer_class = trainer_class
        self.trainer_defaults = trainer_defaults or {}
        self.seed_everything_default = seed_everything_default
        self.subclass_mode_model = subclass_mode_model
        self.subclass_mode_data = subclass_mode_data

        main_kwargs, subparser_kwargs = self._setup_parser_kwargs(
            parser_kwargs or {},  # type: ignore  # github.com/python/mypy/issues/6463
            {"description": description, "env_prefix": env_prefix, "default_env": env_parse},
        )
        self.setup_parser(run, main_kwargs, subparser_kwargs)
        self.parse_arguments(self.parser)

        self.subcommand = self.config["subcommand"] if run else None

        seed = self._get(self.config, "seed_everything")
        if seed is not None:
            seed_everything(seed, workers=True)

        self.before_instantiate_classes()
        self.instantiate_classes()

        if self.subcommand is not None:
            self._run_subcommand(self.subcommand)

    def _setup_parser_kwargs(
        self, kwargs: Dict[str, Any], defaults: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if kwargs.keys() & self.subcommands().keys():
            # `kwargs` contains arguments per subcommand
            return defaults, kwargs
        main_kwargs = defaults
        main_kwargs.update(kwargs)
        return main_kwargs, {}

    def init_parser(self, **kwargs: Any) -> LightningArgumentParser:
        """Method that instantiates the argument parser."""
        return LightningArgumentParser(**kwargs)

    def setup_parser(
        self, add_subcommands: bool, main_kwargs: Dict[str, Any], subparser_kwargs: Dict[str, Any]
    ) -> None:
        """Initialize and setup the parser, subcommands, and arguments."""
        self.parser = self.init_parser(**main_kwargs)
        if add_subcommands:
            self._subcommand_method_arguments: Dict[str, List[str]] = {}
            self._add_subcommands(self.parser, **subparser_kwargs)
        else:
            self._add_arguments(self.parser)

    def add_default_arguments_to_parser(self, parser: LightningArgumentParser) -> None:
        """Adds default arguments to the parser."""
        parser.add_argument(
            "--seed_everything",
            type=Optional[int],
            default=self.seed_everything_default,
            help="Set to an int to run seed_everything with this value before classes instantiation",
        )

    def add_core_arguments_to_parser(self, parser: LightningArgumentParser) -> None:
        """Adds arguments from the core classes to the parser."""
        parser.add_lightning_class_args(self.trainer_class, "trainer")
        trainer_defaults = {"trainer." + k: v for k, v in self.trainer_defaults.items() if k != "callbacks"}
        parser.set_defaults(trainer_defaults)
        parser.add_lightning_class_args(self.model_class, "model", subclass_mode=self.subclass_mode_model)
        if self.datamodule_class is not None:
            parser.add_lightning_class_args(self.datamodule_class, "data", subclass_mode=self.subclass_mode_data)

    def _add_arguments(self, parser: LightningArgumentParser) -> None:
        # default + core + custom arguments
        self.add_default_arguments_to_parser(parser)
        self.add_core_arguments_to_parser(parser)
        self.add_arguments_to_parser(parser)
        self.link_optimizers_and_lr_schedulers(parser)

    def add_arguments_to_parser(self, parser: LightningArgumentParser) -> None:
        """Implement to add extra arguments to the parser or link arguments.

        Args:
            parser: The parser object to which arguments can be added
        """

    @staticmethod
    def subcommands() -> Dict[str, Set[str]]:
        """Defines the list of available subcommands and the arguments to skip."""
        return {
            "fit": {"model", "train_dataloaders", "train_dataloader", "val_dataloaders", "datamodule"},
            "validate": {"model", "dataloaders", "val_dataloaders", "datamodule"},
            "test": {"model", "dataloaders", "test_dataloaders", "datamodule"},
            "predict": {"model", "dataloaders", "datamodule"},
            "tune": {"model", "train_dataloaders", "train_dataloader", "val_dataloaders", "datamodule"},
        }

    def _add_subcommands(self, parser: LightningArgumentParser, **kwargs: Any) -> None:
        """Adds subcommands to the input parser."""
        parser_subcommands = parser.add_subcommands()
        # the user might have passed a builder function
        trainer_class = (
            self.trainer_class if isinstance(self.trainer_class, type) else class_from_function(self.trainer_class)
        )
        # register all subcommands in separate subcommand parsers under the main parser
        for subcommand in self.subcommands():
            subcommand_parser = self._prepare_subcommand_parser(trainer_class, subcommand, **kwargs.get(subcommand, {}))
            fn = getattr(trainer_class, subcommand)
            # extract the first line description in the docstring for the subcommand help message
            description = _get_short_description(fn)
            parser_subcommands.add_subcommand(subcommand, subcommand_parser, help=description)

    def _prepare_subcommand_parser(self, klass: Type, subcommand: str, **kwargs: Any) -> LightningArgumentParser:
        parser = self.init_parser(**kwargs)
        self._add_arguments(parser)
        # subcommand arguments
        skip = self.subcommands()[subcommand]
        added = parser.add_method_arguments(klass, subcommand, skip=skip)
        # need to save which arguments were added to pass them to the method later
        self._subcommand_method_arguments[subcommand] = added
        return parser

    @staticmethod
    def link_optimizers_and_lr_schedulers(parser: LightningArgumentParser) -> None:
        """Creates argument links for optimizers and learning rate schedulers that specified a ``link_to``."""
        for key, (class_type, link_to) in parser.optimizers_and_lr_schedulers.items():
            if link_to == "AUTOMATIC":
                continue
            if isinstance(class_type, tuple):
                parser.link_arguments(key, link_to)
            else:
                add_class_path = _add_class_path_generator(class_type)
                parser.link_arguments(key, link_to, compute_fn=add_class_path)

    def parse_arguments(self, parser: LightningArgumentParser) -> None:
        """Parses command line arguments and stores it in ``self.config``."""
        self.config = parser.parse_args()

    def before_instantiate_classes(self) -> None:
        """Implement to run some code before instantiating the classes."""

    def instantiate_classes(self) -> None:
        """Instantiates the classes and sets their attributes."""
        self.config_init = self.parser.instantiate_classes(self.config)
        self.datamodule = self._get(self.config_init, "data")
        self.model = self._get(self.config_init, "model")
        self._add_configure_optimizers_method_to_model(self.subcommand)
        self.trainer = self.instantiate_trainer()

    def instantiate_trainer(self, **kwargs: Any) -> Trainer:
        """Instantiates the trainer.

        Args:
            kwargs: Any custom trainer arguments.
        """
        extra_callbacks = [self._get(self.config_init, c) for c in self._parser(self.subcommand).callback_keys]
        trainer_config = {**self._get(self.config_init, "trainer"), **kwargs}
        return self._instantiate_trainer(trainer_config, extra_callbacks)

    def _instantiate_trainer(self, config: Dict[str, Any], callbacks: List[Callback]) -> Trainer:
        config["callbacks"] = config["callbacks"] or []
        config["callbacks"].extend(callbacks)
        if "callbacks" in self.trainer_defaults:
            if isinstance(self.trainer_defaults["callbacks"], list):
                config["callbacks"].extend(self.trainer_defaults["callbacks"])
            else:
                config["callbacks"].append(self.trainer_defaults["callbacks"])
        if self.save_config_callback and not config["fast_dev_run"]:
            config_callback = self.save_config_callback(
                self.parser, self.config, self.save_config_filename, overwrite=self.save_config_overwrite
            )
            config["callbacks"].append(config_callback)
        return self.trainer_class(**config)

    def _parser(self, subcommand: Optional[str]) -> ArgumentParser:
        if subcommand is None:
            return self.parser
        # return the subcommand parser for the subcommand passed
        action_subcommands = [a for a in self.parser._actions if isinstance(a, _ActionSubCommands)]
        action_subcommand = action_subcommands[0]
        return action_subcommand._name_parser_map[subcommand]

    def _add_configure_optimizers_method_to_model(self, subcommand: Optional[str]) -> None:
        """Adds to the model an automatically generated ``configure_optimizers`` method.

        If a single optimizer and optionally a scheduler argument groups are added to the parser as 'AUTOMATIC', then a
        `configure_optimizers` method is automatically implemented in the model class.
        """
        parser = self._parser(subcommand)
        optimizers_and_lr_schedulers = parser.optimizers_and_lr_schedulers

        def get_automatic(class_type: Union[Type, Tuple[Type, ...]]) -> List[str]:
            automatic = []
            for key, (base_class, link_to) in optimizers_and_lr_schedulers.items():
                if not isinstance(base_class, tuple):
                    base_class = (base_class,)
                if link_to == "AUTOMATIC" and any(issubclass(c, class_type) for c in base_class):
                    automatic.append(key)
            return automatic

        optimizers = get_automatic(Optimizer)
        lr_schedulers = get_automatic(LRSchedulerTypeTuple)

        if len(optimizers) == 0:
            return

        if len(optimizers) > 1 or len(lr_schedulers) > 1:
            raise MisconfigurationException(
                f"`{self.__class__.__name__}.add_configure_optimizers_method_to_model` expects at most one optimizer "
                f"and one lr_scheduler to be 'AUTOMATIC', but found {optimizers+lr_schedulers}. In this case the user "
                "is expected to link the argument groups and implement `configure_optimizers`, see "
                "https://pytorch-lightning.readthedocs.io/en/stable/common/lightning_cli.html"
                "#optimizers-and-learning-rate-schedulers"
            )

        if is_overridden("configure_optimizers", self.model):
            warnings._warn(
                f"`{self.model.__class__.__name__}.configure_optimizers` will be overridden by "
                f"`{self.__class__.__name__}.add_configure_optimizers_method_to_model`."
            )

        optimizer_class = optimizers_and_lr_schedulers[optimizers[0]][0]
        optimizer_init = self._get(self.config_init, optimizers[0], default={})
        if not isinstance(optimizer_class, tuple):
            optimizer_init = _global_add_class_path(optimizer_class, optimizer_init)
        lr_scheduler_init = None
        if lr_schedulers:
            lr_scheduler_class = optimizers_and_lr_schedulers[lr_schedulers[0]][0]
            lr_scheduler_init = self._get(self.config_init, lr_schedulers[0], default={})
            if not isinstance(lr_scheduler_class, tuple):
                lr_scheduler_init = _global_add_class_path(lr_scheduler_class, lr_scheduler_init)

        def configure_optimizers(
            self: LightningModule,
        ) -> Union[Optimizer, Tuple[List[Optimizer], List[LRSchedulerType]]]:
            optimizer = instantiate_class(self.parameters(), optimizer_init)
            if not lr_scheduler_init:
                return optimizer
            lr_scheduler = instantiate_class(optimizer, lr_scheduler_init)
            return [optimizer], [lr_scheduler]

        self.model.configure_optimizers = MethodType(configure_optimizers, self.model)

    def _get(self, config: Dict[str, Any], key: str, default: Optional[Any] = None) -> Any:
        """Utility to get a config value which might be inside a subcommand."""
        if self.subcommand is not None:
            return config[self.subcommand].get(key, default)
        return config.get(key, default)

    def _run_subcommand(self, subcommand: str) -> None:
        """Run the chosen subcommand."""
        before_fn = getattr(self, f"before_{subcommand}", None)
        if callable(before_fn):
            before_fn()

        default = getattr(self.trainer, subcommand)
        fn = getattr(self, subcommand, default)
        fn_kwargs = self._prepare_subcommand_kwargs(subcommand)
        fn(**fn_kwargs)

        after_fn = getattr(self, f"after_{subcommand}", None)
        if callable(after_fn):
            after_fn()

    def _prepare_subcommand_kwargs(self, subcommand: str) -> Dict[str, Any]:
        """Prepares the keyword arguments to pass to the subcommand to run."""
        fn_kwargs = {
            k: v for k, v in self.config_init[subcommand].items() if k in self._subcommand_method_arguments[subcommand]
        }
        fn_kwargs["model"] = self.model
        if self.datamodule is not None:
            fn_kwargs["datamodule"] = self.datamodule
        return fn_kwargs


def _global_add_class_path(class_type: Type, init_args: Dict[str, Any]) -> Dict[str, Any]:
    return {"class_path": class_type.__module__ + "." + class_type.__name__, "init_args": init_args}


def _add_class_path_generator(class_type: Type) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    def add_class_path(init_args: Dict[str, Any]) -> Dict[str, Any]:
        return _global_add_class_path(class_type, init_args)

    return add_class_path


def instantiate_class(args: Union[Any, Tuple[Any, ...]], init: Dict[str, Any]) -> Any:
    """Instantiates a class with the given args and init.

    Args:
        args: Positional arguments required for instantiation.
        init: Dict of the form {"class_path":...,"init_args":...}.

    Returns:
        The instantiated class object.
    """
    kwargs = init.get("init_args", {})
    if not isinstance(args, tuple):
        args = (args,)
    class_module, class_name = init["class_path"].rsplit(".", 1)
    module = __import__(class_module, fromlist=[class_name])
    args_class = getattr(module, class_name)
    return args_class(*args, **kwargs)


def _get_short_description(component: object) -> Optional[str]:
    parse = import_docstring_parse("LightningCLI(run=True)")
    try:
        docstring = parse(component.__doc__)
        return docstring.short_description
    except ValueError:
        rank_zero_warn(f"Failed parsing docstring for {component}")
