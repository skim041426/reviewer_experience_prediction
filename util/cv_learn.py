"""
:author: Matt Mulholland (mulhodm@gmail.com)
:date: 10/14/2015

Command-line utility utilizing the RunCVExperiments class, which enables
one to run cross-validation experiments incrementally with a number of
different machine learning algorithms and parameter customizations, etc.
"""
import logging
from copy import copy
from json import dump
from os import makedirs
from itertools import chain
from os.path import (join,
                     isdir,
                     isfile,
                     dirname,
                     realpath)
from warnings import filterwarnings

import numpy as np
import pandas as pd
from typing import (Any,
                    Dict,
                    List,
                    Union,
                    Iterable,
                    Optional)
from pymongo import ASCENDING
from skll.metrics import kappa
from scipy.stats import pearsonr
from sklearn.metrics import make_scorer
from schema import (Or,
                    And,
                    Schema,
                    SchemaError,
                    Optional as Default)
from pymongo.collection import Collection
from sklearn.cluster import MiniBatchKMeans
from pymongo.errors import ConnectionFailure
from sklearn.grid_search import (GridSearchCV,
                                 ParameterGrid)
from sklearn.naive_bayes import (BernoulliNB,
                                 MultinomialNB)
from argparse import (ArgumentParser,
                      ArgumentDefaultsHelpFormatter)
from sklearn.cross_validation import StratifiedKFold
from sklearn.feature_extraction import (FeatureHasher,
                                        DictVectorizer)
from sklearn.linear_model import (Perceptron,
                                  PassiveAggressiveRegressor)

from src.mongodb import connect_to_db
from src import (LABELS,
                 Scorer,
                 Learner,
                 Numeric,
                 formatter,
                 Vectorizer,
                 VALID_GAMES,
                 LEARNER_DICT,
                 LABELS_STRING,
                 experiments as ex,
                 LEARNER_DICT_KEYS,
                 parse_games_string,
                 LEARNER_ABBRS_DICT,
                 OBJ_FUNC_ABBRS_DICT,
                 LEARNER_ABBRS_STRING,
                 OBJ_FUNC_ABBRS_STRING,
                 parse_learners_string,
                 find_default_param_grid,
                 parse_non_nlp_features_string)
from src.datasets import (validate_bin_ranges,
                          get_bin_ranges_helper)

# Filter out warnings since there will be a lot of
# "UndefinedMetricWarning" warnings when running `RunCVExperiments`
filterwarnings("ignore")

# Set up logger
logger = logging.getLogger(__name__)
logging_debug = logging.DEBUG
logger.setLevel(logging_debug)
loginfo = logger.info
logerr = logger.error
logdebug = logger.debug
sh = logging.StreamHandler()
sh.setLevel(logging_debug)
sh.setFormatter(formatter)
logger.addHandler(sh)


class CVConfig(object):
    """
    Class for representing a set of configuration options for use with
    the `RunCVExperiments` class.
    """

    # Default value to use for the `hashed_features` parameter if 0 is
    # passed in.
    _n_features_feature_hashing = 2 ** 18

    def __init__(self,
                 db: Collection,
                 games: set,
                 learners: List[str],
                 param_grids: dict,
                 training_rounds: int,
                 training_samples_per_round: int,
                 grid_search_samples_per_fold: int,
                 non_nlp_features: set,
                 prediction_label: str,
                 objective: str = None,
                 data_sampling: str = 'even',
                 grid_search_folds: int = 5,
                 hashed_features: Optional[int] = None,
                 nlp_features: bool = True,
                 bin_ranges: Optional[list] = None,
                 lognormal: bool = False,
                 power_transform: Optional[float] = None,
                 majority_baseline: bool = True,
                 rescale: bool = True) -> 'CVConfig':
        """
        Initialize object.

        :param db: MongoDB database collection object
        :type db: Collection
        :param games: set of games to use for training models
        :type games: set
        :param learners: list of abbreviated names corresponding to
                         the available learning algorithms (see
                         `src.LEARNER_ABBRS_DICT`, etc.)
        :type learners: list
        :param param_grids: list of dictionaries of parameters mapped
                            to lists of values (must be aligned with
                            list of learners)
        :type param_grids: dict
        :param training_rounds: number of training rounds to do (in
                                addition to the grid search round)
        :type training_rounds: int
        :param training_samples_per_round: number of training samples
                                           to use in each training round
        :type training_samples_per_round: int
        :param grid_search_samples_per_fold: number of samples to use
                                             for each grid search fold
        :type grid_search_samples_per_fold: int
        :param non_nlp_features: set of non-NLP features to add into the
                                 feature dictionaries 
        :type non_nlp_features: set
        :param prediction_label: feature to predict
        :type prediction_label: str
        :param objective: objective function to use in ranking the runs;
                          if left unspecified, the objective will be
                          decided in `GridSearchCV` and will be either
                          accuracy for classification or r2 for
                          regression
        :type objective: str or None
        :param data_sampling: how the data should be sampled (i.e.,
                              either 'even' or 'stratified')
        :type data_sampling: str
        :param grid_search_folds: number of grid search folds to use
                                  (default: 5)
        :type grid_search_folds: int
        :param hashed_features: use FeatureHasher in place of
                                DictVectorizer and use the given number
                                of features (must be positive number or
                                0, which will set it to the default
                                number of features for feature hashing)
        :type hashed_features: int
        :param nlp_features: include NLP features (default: True)
        :type nlp_features: bool
        :param bin_ranges: list of tuples representing the maximum and
                           minimum values corresponding to bins (for
                           splitting up the distribution of prediction
                           label values)
        :type bin_ranges: list or None
        :param lognormal: transform raw label values using `ln` (default:
                          False)
        :type lognormal: bool
        :param power_transform: power by which to transform raw label
                                values (default: False)
        :type power_transform: float or None
        :param majority_baseline: evaluate a majority baseline model
        :type majority_baseline: bool
        :param rescale: whether or not to rescale the predicted values
                        based on the input value distribution (defaults
                        to True, but set to False if this is a
                        classification experiment)
        :type rescale: bool

        :returns: instance of `CVConfig` class
        :rtype: CVConfig

        :raises ValueError: if the input parameters result in conflicts
                            or are invalid
        """

        # Get dicionary of parameters (but remove "self" since that
        # doesn't need to be validated and remove values set to None
        # since they will be dealt with automatically)
        params = dict(locals())
        del params['self']
        for param in list(params):
            if params[param] is None:
                del params[param]

        # Schema
        exp_schema = Schema(
            {'db': Collection,
             'games': And(set, lambda x: x.issubset(VALID_GAMES)),
             'learners': And([str],
                             lambda learners: all(learner in LEARNER_DICT_KEYS
                                                  for learner in learners)),
             'param_grids': [{str: list}],
             'training_rounds': And(int, lambda x: x > 1),
             'training_samples_per_round': And(int, lambda x: x > 0),
             'grid_search_samples_per_fold': And(int, lambda x: x > 1),
             'non_nlp_features': And({str}, lambda x: LABELS.issuperset(x)),
             'prediction_label':
                 And(str,
                     lambda x: x in LABELS and not x in params['non_nlp_features']),
             Default('objective', default=None): lambda x: x in OBJ_FUNC_ABBRS_DICT,
             Default('data_sampling', default='even'):
                And(str, lambda x: x in ex.ExperimentalData.sampling_options),
             Default('grid_search_folds', default=5): And(int, lambda x: x > 1),
             Default('hashed_features', default=None):
                Or(None,
                   lambda x: not isinstance(x, bool)
                             and isinstance(x, int)
                             and x > -1),
             Default('nlp_features', default=True): bool,
             Default('bin_ranges', default=None):
                Or(None,
                   And([(float, float)],
                       lambda x: validate_bin_ranges(x) is None)),
             Default('lognormal', default=False): bool,
             Default('power_transform', default=None):
                Or(None, And(float, lambda x: x != 0.0)),
             Default('majority_baseline', default=True): bool,
             Default('rescale', default=True): bool
             }
            )

        # Validate the schema
        try:
            self.validated = exp_schema.validate(params)
        except (ValueError, SchemaError) as e:
            msg = ('The set of passed-in parameters was not able to be '
                   'validated and/or the bin ranges values, if specified, were'
                   ' not able to be validated.')
            logger.error('{0}:\n\n{1}'.format(msg, e))
            raise e

        # Set up the experiment
        self._further_validate_and_setup()

    def _further_validate_and_setup(self) -> None:
        """
        Further validate the experiment's configuration settings and set
        up certain configuration settings, such as setting the total
        number of hashed features to use, etc.

        :returns: None
        :rtype: None
        """

        # Make sure parameters make sense/are valid
        if len(self.validated['learners']) != len(self.validated['param_grids']):
            raise SchemaError(autos=None,
                              errors='The lists of of learners and parameter '
                                     'grids must be the same size.')
        if (self.validated['hashed_features'] is not None
            and self.validated['hashed_features'] == 0):
                self.validated['hashed_features'] = self._n_features_feature_hashing
        if self.validated['lognormal'] and self.validated['power_transform']:
            raise SchemaError(autos=None,
                              errors='Both "lognormal" and "power_transform" '
                                     'were set simultaneously.')
        if len(self.validated['learners']) != len(self.validated['param_grids']):
            raise SchemaError(autos=None,
                              errors='The "learners" and "param_grids" '
                                      'parameters were both set and the '
                                      'lengths of the lists are unequal.')


class RunCVExperiments(object):
    """
    Class for conducting sets of incremental cross-validation
    experiments.
    """

    # Constants
    _default_cursor_batch_size = 50
    #_learners_requiring_classes = frozenset({'BernoulliNB',
    #                                         'MultinomialNB',
    #                                         'Perceptron'})
    _no_introspection_learners = frozenset({'MiniBatchKMeans',
                                            'PassiveAggressiveRegressor'})

    def __init__(self, config: CVConfig) -> 'RunCVExperiments':
        """
        Initialize object.

        :param config: an `CVConfig` instance containing configuration
                       options relating to the experiment, etc.
        :type config: CVConfig
        """

        # Experiment configuration settings
        self._cfg = pd.Series(config.validated)

        # Games
        if not self._cfg.games:
            raise ValueError('The set of games must be greater than zero!')
        self._games_string = ', '.join(self._cfg.games)

        # Templates for report file names
        self._report_name_template = ('{0}_{1}_{2}_{3}.csv'
                                      .format(self._games_string, '{0}', '{1}', '{2}'))
        self._stats_name_template = (self._report_name_template
                                     .format('{0}', 'stats', '{1}'))
        self._model_weights_name_template = (self._report_name_template
                                             .format('{0}', 'model_weights', '{1}'))
        if self._cfg.majority_baseline:
            self._majority_baseline_report_name = \
                '{0}_majority_baseline_model_stats.csv'.format(self._games_string)
        if self._cfg.lognormal or self._cfg.power_transform:
            self._transformation_string = ('ln' if self._cfg.lognormal
                                           else 'x**{0}'.format(self._cfg.power_transform))
        else:
            self._transformation_string = 'None'

        # Objective function
        if not self._cfg.objective in OBJ_FUNC_ABBRS_DICT:
            raise ValueError('Unrecognized objective function used: {0}. '
                             'These are the available objective functions: {1}.'
                             .format(self._cfg.objective, OBJ_FUNC_ABBRS_STRING))

        # Data-set- and database-related variables
        self._batch_size = \
            (self._cfg.training_samples_per_round
             if self._cfg.training_samples_per_round < self._default_cursor_batch_size
             else self._default_cursor_batch_size)
        self._projection = {'_id': 0}
        if not self._cfg.nlp_features:
            self._projection['nlp_features'] = 0
        self._data = self._generate_experimental_data()

        # Create and fit a vectorizer with all possible samples
        train_ids = list(chain(*self._data.training_set))
        grid_search_ids = list(chain(*self._data.grid_search_set))
        all_ids = train_ids + grid_search_ids
        self._vec = self._make_vectorizer(all_ids,
                                          hashed_features=self._cfg.hashed_features)

        # Store all of the labels used for grid search and training
        self._y_all = []

        # Learner-related variables
        self._param_grids = [list(ParameterGrid(param_grid)) for param_grid
                             in self._cfg.param_grids]
        self._learners = [LEARNER_DICT[learner] for learner in self._cfg.learners]
        self._learner_names = [LEARNER_ABBRS_DICT[learner] for learner
                               in self._cfg.learners]

        # Do grid search round
        logger.info('Executing parameter grid search learning round...')
        self._gs_cv_folds = None
        self._learner_gs_cv_dict = self._do_grid_search_round()
        
        # Make a dictionary mapping each learner name to a list of
        # individual copies of the grid search cross-validation round's
        # best estimator instances, with the length of the list equal to
        # the number of folds in the training set since each of these
        # estimator instances will be incrementally improved upon and
        # evaluated
        self._cv_learners = [[copy(self._learner_gs_cv_dict[learner_name])
                              for learner_name in self._learner_names]
                             for _ in range(self._data.folds)]
        self._cv_learners = dict(zip(self._learner_names,
                                     zip(*self._cv_learners)))
        self._cv_learners = {k: list(v) for k, v in self._cv_learners.items()}

        # Make a list of empty lists corresponding to each learner,
        # which will be used to hold the performance stats for each
        # cross-validation leave-one-fold-out sub-experiment
        self._cv_learner_stats = [[] for _ in self._cfg.learners]

        # Do incremental learning experiments
        logger.info('Incremental learning cross-validation experiments '
                    'initialized...')
        self._do_training_cross_validation()
        self._training_cross_validation_experiment_stats = \
            ex.aggregate_cross_validation_experiments_stats(self._cv_learner_stats)

        # Generate statistics for the majority baseline model
        if self._cfg.majority_baseline:
            self._majority_baseline_stats = self._evaluate_majority_baseline_model()

    def _resolve_objective_function(self) -> Scorer:
        """
        Resolve value of parameter to be passed in to the `scoring`
        parameter in `GridSearchCV`, which can be `None`, a string, or a
        callable.

        :returns: a value to pass into the `scoring` parameter in
                  `GridSearchCV`
        :rtype: str, None, callable
        """

        if not self._cfg.objective:
            return None

        if self._cfg.objective == 'pearson_r':
            scorer = make_scorer(pearsonr)
        elif self._cfg.objective.startswith('uwk'):
            if self._cfg.objective == 'uwk':
                scorer = make_scorer(kappa)
            else:
                scorer = make_scorer(kappa, allow_off_by_one=True)
        elif self._cfg.objective.startswith('lwk'):
            if self._cfg.objective == 'lwk':
                scorer = make_scorer(kappa, weights='linear')
            else:
                scorer = make_scorer(kappa, weights='linear',
                                     allow_off_by_one=True)
        elif self._cfg.objective.startswith('lwk'):
            if self._cfg.objective == 'lwk':
                scorer = make_scorer(kappa, weights='quadratic')
            else:
                scorer = make_scorer(kappa, weights='quadratic',
                                     allow_off_by_one=True)
        else:
            scorer = self._cfg.objective

        return scorer

    def _generate_experimental_data(self):
        """
        Call `src.experiments.ExperimentalData` to generate a set of
        data to be used for grid search, training, etc.
        """

        logger.info('Extracting dataset...')
        return ex.ExperimentalData(db=self._cfg.db,
                                   prediction_label=self._cfg.prediction_label,
                                   games=self._cfg.games,
                                   folds=self._cfg.training_rounds,
                                   fold_size=self._cfg.training_samples_per_round,
                                   grid_search_folds=self._cfg.grid_search_folds,
                                   grid_search_fold_size=
                                       self._cfg.grid_search_samples_per_fold,
                                   sampling=data_sampling,
                                   lognormal=self._cfg.lognormal,
                                   power_transform=self._cfg.power_transform,
                                   bin_ranges=self._cfg.bin_ranges,
                                   batch_size=self._batch_size)

    def _make_vectorizer(self, ids: List[str],
                         hashed_features: Optional[int] = None) -> Vectorizer:
        """
        Make a vectorizer.

        :param ids: a list of sample ID strings with which to fit the
                    vectorizer
        :type ids: list
        :param hashed_features: if feature hasing is being used, provide
                                the number of features to use;
                                otherwise, the value should be False
        :type hashed_features: bool or int

        :returns: a vectorizer, i.e., DictVectorizer or FeatureHasher
        :rtype: Vectorizer

        :raises ValueError: if the value of `hashed_features` is not
                            greater than zero or `ids` is empty
        """

        if hashed_features:
            if hashed_features < 1:
                raise ValueError('The value of "hashed_features" should be a '
                                 'positive integer, preferably a very large '
                                 'integer.')
            vec = FeatureHasher(n_features=hashed_features,
                                non_negative=True)
        else:
            vec = DictVectorizer(sparse=True)

        if not ids:
            raise ValueError('The "ids" parameter is empty.')

        self._fit_vectorizer(vec, ids)

        return vec

    def _fit_vectorizer(self, vec: Vectorizer, ids: List[str]) -> Vectorizer:
        """
        Fit a vectorizer with all samples corresponding to the given
        list of IDs.
        
        :param vec: vectorizer instance
        :type vec: DictVectorizer or FeatureHasher
        :param ids: list of sample ID strings
        :type ids: list

        :returns: None
        :rtype: None
        """

        vec.fit(self._generate_samples(ids, 'x'))

        return vec

    def _generate_samples(ids: List[str], key: Optional[str] = None) \
        -> Iterable[Union[Dict[str, Any], str, Numeric]]:
        """
        Generate feature dictionaries for the review samples in the
        given cursor.

        Provides a lower-memory way of fitting a vectorizer, for
        example.

        :param ids: list of ID strings
        :type ids: list
        :param key: yield only the value of the specified key (if a key
                    is specified), can be the following values: 'y',
                    'x', or 'id'
        :type key: str or None

        :yields: feature dictionary
        :ytype: dict, str, int, float, etc.
        """
        for doc in ex.make_cursor(self._cfg.db,
                                  projection=self._projection,
                                  batch_size=self._batch_size,
                                  id_strings=ids):
            sample = ex.get_data_point(doc,
                                       prediction_label=self._cfg.prediction_label,
                                       nlp_features=self._cfg.nlp_features,
                                       non_nlp_features=self._cfg.non_nlp_features,
                                       lognormal=self._cfg.lognormal,
                                       power_transform=self._cfg.power_transform,
                                       bin_ranges=self._cfg.bin_ranges)

            # Either yield the sample given the specified key or yield
            # the whole sample
            yield sample.get(key, sample)

    def _do_grid_search_round(self) -> Dict[str, GridSearchCV]:
        """
        Do grid search round.

        :returns: dictionary of learner names mapped to already-fitted
                  `GridSearchCV` instances, including attributes such as
                  `best_estimator_`
        :rtype: dict
        """

        # Get the data to use, vectorizing the sample feature dictionaries
        grid_search_all_ids = list(chain(*self._data.grid_search_set))
        y_train = list(self._generate_samples(grid_search_all_ids, 'y'))
        X_train = self._vec.transform(self._generate_samples(grid_search_all_ids, 'x'))

        # Update `self._y_all` with all of the samples used during the
        # grid search round
        self._y_all.extend(y_train)

        # Make a `StratifiedKFold` object using the list of labels
        # NOTE: This will effectively redistribute the samples in the
        # various grid search folds, but it will maintain the
        # distribution of labels. Furthermore, due to the use of the
        # `RandomState` object, it should always happen in the exact
        # same way.
        prng = np.random.RandomState(12345)
        self._gs_cv_folds = StratifiedKFold(y=y_train,
                                            n_folds=self._data.grid_search_folds,
                                            shuffle=True,
                                            random_state=prng)

        # Iterate over the learners/parameter grids, executing the grid search
        # cross-validation for each
        logger.info('Doing a grid search cross-validation round with {0} as '
                    'the number of folds.'
                    .format(self._data.grid_search_folds))
        learner_gs_cv_dict = {}
        for learner, learner_name, param_grid in zip(self._learners,
                                                     self._learner_names,
                                                     self._cfg.param_grids):

            # If the learner is `MiniBatchKMeans`, set the `batch_size`
            # parameter to the number of training samples
            if learner_name == 'MiniBatchKMeans':
                param_grid['batch_size'] = len(y_train)

            # Make `GridSearchCV` instance
            folds_diff = self._cfg.grid_search_folds - self._data.grid_search_folds
            if (self._data.grid_search_folds < 2
                or folds_diff/self._cfg.grid_search_folds > 0.25):
                msg = ('Either there weren\'t enough folds after collecting '
                       'data (via `ExperimentalData`) to do the grid search '
                       'round or the number of folds had to be reduced to such'
                       ' a degree that it would mean a +25\% reduction in the '
                       'total number of folds used during the grid search '
                       'round.')
                logger.error(msg)
                raise ValueError(msg)
            gs_cv = GridSearchCV(learner(),
                                 param_grid,
                                 cv=self._gs_cv_folds,
                                 scoring=self._resolve_objective_function())

            # Do the grid search cross-validation
            gs_cv.fit(X_train, y_train)
            learner_gs_cv_dict[learner_name] = gs_cv

        return learner_gs_cv_dict

    def _do_training_cross_validation(self) -> None:
        """
        Do cross-validation with training data. Each train/test split will
        represent an individual incremental learning experiment, i.e., starting
        with the best estimator from the grid search round, learn little by
        little from batches of training samples and evaluate on the held-out
        partition of data.

        :returns: None
        :rtype: None
        """

        # For each fold of the training set, train on all of the other
        # folds and evaluate on the one left out fold
        y_training_set_all = []
        for i, held_out_fold in enumerate(self._data.training_set):

            # Use each training fold (except for the held-out set) to
            # incrementally build up the model
            training_folds = self._data.training_set[:i] + self_data.training_set[i + 1:]
            y_train_all = []
            for training_fold in training_folds:

                # Get the training data
                y_train = list(self._generate_samples(training_fold, 'y'))
                X_train = self._vec.transform(self._generate_samples(training_fold, 'x'))

                # Store the actual input values so that rescaling can be
                # done later
                y_train_all.extend(y_train)

                # Iterate over the learners
                for learner_name in self._learner_names:

                    # Partially fit each estimator with the new training data
                    self._cv_learners[learner_name][i].partial_fit(X_train, y_train)

            # Get mean and standard deviation for actual values
            y_train_all = np.array(y_train_all)
            y_train_mean = y_train_all.mean()
            y_train_std = y_train_all.std()

            # Get test data
            y_test = list(self._generate_samples(held_out_fold, 'y'))
            X_test = self._vec.transform(self._generate_samples(held_out_fold, 'x'))

            # Add test labels to `y_training_set_all`
            y_training_set_all.extend(y_test)

            # Make predictions with the modified estimators
            for j, learner_name in enumerate(self._learner_names):

                # Make predictions with the given estimator,rounding the
                # predictions
                y_test_preds = np.round(self._cv_learners[learner_name][i]
                                        .predict(X_test))

                # Rescale the predicted values based on the
                # mean/standard deviation of the actual values and
                # fit the predicted values within the original scale
                # (i.e., no predicted values should be outside the range
                # of possible values)
                y_test_preds_dict = \
                    ex.rescale_preds_and_fit_in_scale(y_test_preds,
                                                      self._data.classes,
                                                      y_train_mean,
                                                      y_train_std)

                if self._cfg.rescale:
                    y_test_preds = y_test_preds_dict['rescaled']
                else:
                    y_test_preds = y_test_preds_dict['fitted_only']

                # Evaluate the predictions and add to list of evaluation
                # reports for each learner
                (self._cv_learner_stats[j]
                 .append(ex.evaluate_predictions_from_learning_round(
                             y_test,
                             y_test_preds,
                             self._data.classes,
                             self._cfg.prediction_label,
                             self._cfg.non_nlp_features,
                             self._cfg.nlp_features,
                             self._cv_learners[learner_name][i],
                             learner_name,
                             self._cfg.games,
                             self._cfg.games,
                             i,
                             len(y_train_all),
                             self._cfg.bin_ranges,
                             self._cfg.rescale,
                             self._transformation_string)))

        # Update `self._y_all` with all of the samples used during the
        # cross-validation
        self._y_all.extend(y_training_set_all)

    def _get_majority_baseline(self) -> np.ndarray:
        """
        Generate a majority baseline array of prediction labels.

        :returns: array of prediction labels
        :rtype: np.ndarray
        """

        self._majority_label = max(set(self._y_all), key=self._y_all.count)
        return np.array([self._majority_label]*len(self._y_all))

    def _evaluate_majority_baseline_model(self) -> pd.Series:
        """
        Evaluate the majority baseline model predictions.

        :returns: a Series containing the majority label system's
                  performance metrics and attributes
        :rtype: pd.Series
        """

        stats_dict = ext.compute_evaluation_metrics(self._y_all,
                                                    self._get_majority_baseline(),
                                                    self._data.classes)
        stats_dict.update({'games' if len(self._cfg.games) > 1 else 'game':
                               self._games_string
                               if VALID_GAMES.difference(self._cfg.games)
                               else 'all_games',
                           'prediction_label': self._cfg.prediction_label,
                           'majority_label': self._majority_label,
                           'learner': 'majority_baseline_model',
                           'transformation': self._transformation_string})
        if self._cfg.bin_ranges:
            stats_dict.update({'bin_ranges': self._cfg.bin_ranges})
        return pd.Series(stats_dict)

    def generate_majority_baseline_report(self, output_path: str) -> None:
        """
        Generate a CSV file reporting on the performance of the
        majority baseline model.

        :param output_path: path to destination directory
        :type: str

        :returns: None
        :rtype: None
        """

        self._majority_baseline_stats.to_csv(join(output_path,
                                                  self._majority_baseline_report_name))

    def generate_learning_reports(self, output_path: str) -> None:
        """
        Generate experimental reports for each run represented in the
        lists of input dataframes.

        The output files will have indices in their names, which simply
        correspond to the sequence in which they occur in the list of
        input dataframes.

        :param output_path: path to destination directory
        :type output_path: str

        :returns: None
        :rtype: None
        """

        for i, cv_learner_series_list in enumerate(self._cv_learner_stats):
            df = pd.DataFrame(cv_learner_series_list)
            learner_name = df['learner'].iloc[0]
            df.to_csv(join(output_path, self._stats_name_template.format(learner_name)),
                      index=False)

    def store_sorted_features(self, model_weights_path: str) -> None:
        """
        Store files with sorted lists of features and their associated
        coefficients from each model (for which introspection like this
        can be done, at least).

        :param model_weights_path: path to directory for model weights
                                   files
        :type model_weights_path: str

        :returns: None
        :rtype: None
        """

        makedirs(model_weights_path, exist_ok=True)

        # Generate feature weights files and a README.json providing
        # the parameters corresponding to each set of feature weights
        params_dict = {}
        for learner_name in self._cv_learners:

            # Skip MiniBatchKMeans/PassiveAggressiveRegressor models
            if learner_name in self._no_introspection_learners:
                continue

            for i, estimator in self._cv_learners[learner_name]:

                # Get dataframe of the features/coefficients
                try:
                    ex.print_model_weights(estimator,
                                           learner_name,
                                           self._data.classes,
                                           self._cfg.games,
                                           self._vec,
                                           join(model_weights_path,
                                                self._model_weights_name_template
                                                .format(learner_name, i + 1)))
                    params_dict.setdefault(learner_name, {})
                    params_dict[learner_name][i] = learner.get_params()
                except ValueError:
                    logger.error('Could not generate features/feature '
                                 'coefficients dataframe for {0}...'
                                 .format(learner_name))

        # Save parameters file also
        if params_dict:
            dump(params_dict,
                 open(join(model_weights_path, 'model_params_readme.json'), 'w'),
                 indent=4)


def main(argv=None):
    parser = ArgumentParser(description='Run incremental learning '
                                        'experiments.',
                            formatter_class=ArgumentDefaultsHelpFormatter,
                            conflict_handler='resolve')
    _add_arg = parser.add_argument
    _add_arg('-g', '--games',
             help='Game(s) to use in experiments; or "all" to use data from '
                  'all games.',
             type=str,
             required=True)
    _add_arg('-out', '--output_dir',
             help='Directory in which to output data related to the results '
                  'of the conducted experiments.',
             type=str,
             required=True)
    _add_arg('--training_rounds',
             help='The maximum number of rounds of learning to conduct (the '
                  'number of rounds will necessarily be limited by the amount'
                  ' of training data and the number of samples used per '
                  'round). Use "0" to do as many rounds as possible.',
             type=int,
             default=0)
    _add_arg('--max_training_samples_per_round',
             help='The maximum number of training samples to use in each '
                  'round.',
             type=int,
             default=100)
    _add_arg('--grid_search_folds',
             help='The maximum number of folds to use in the grid search '
                  'round.',
             type=int,
             default=5)
    _add_arg('--max_grid_search_samples_per_fold',
             help='The maximum number of training samples to use in each grid '
                  'search fold.',
             type=int,
             default=1000)
    _add_arg('-label', '--prediction_label',
             help='Label to predict.',
             choices=LABELS,
             default='total_game_hours_bin')
    _add_arg('-non_nlp', '--non_nlp_features',
             help='Comma-separated list of non-NLP features to combine with '
                  'the NLP features in creating a model. Use "all" to use all'
                  ' available features, "none" to use no non-NLP features. If'
                  ' --only_non_nlp_features is used, NLP features will be '
                  'left out entirely.',
             type=str,
             default='none')
    _add_arg('-only_non_nlp', '--only_non_nlp_features',
             help="Don't use any NLP features.",
             action='store_true',
             default=False)
    _add_arg('--data_sampling',
             help="Method used for sampling the data.",
             choices=ex.ExperimentalData.sampling_options,
             default='even')
    _add_arg('-l', '--learners',
             help='Comma-separated list of learning algorithms to try. Refer '
                  'to list of learners above to find out which abbreviations '
                  'stand for which learners. Set of available learners: {0}. '
                  'Use "all" to include all available learners.'
                  .format(LEARNER_ABBRS_STRING),
             type=str,
             default='all')
    _add_arg('-bin', '--nbins',
             help='Number of bins to split up the distribution of prediction '
                  'label values into. Use 0 (or don\'t specify) if the values'
                  ' should not be collapsed into bins. Note: Only use this '
                  'option (and --bin_factor below) if the prediction labels '
                  'are numeric.',
             type=int,
             default=0)
    _add_arg('-factor', '--bin_factor',
             help='Factor by which to multiply the size of each bin. Defaults'
                  ' to 1.0 if --nbins is specified.',
             type=float,
             required=False)
    _add_arg('--lognormal',
             help='Transform raw label values with log before doing anything '
                  'else, whether it be binning the values or learning from '
                  'them.',
             action='store_true',
             default=False)
    _add_arg('--power_transform',
             help='Transform raw label values via `x**power` where `power` is'
                  ' the value specified and `x` is the raw label value before'
                  ' doing anything else, whether it be binning the values or '
                  'learning from them.',
             type=float,
             default=None)
    _add_arg('-feature_hasher', '--use_feature_hasher',
             help='Use FeatureHasher to be more memory-efficient.',
             action='store_true',
             default=False)
    _add_arg('-rescale', '--rescale_predictions',
             help='Rescale prediction values based on the mean/standard '
                  'deviation of the input values and fit all predictions into '
                  'the expected scale. Don\'t use if the experiment involves '
                  'labels rather than numeric values.',
             action='store_true',
             default=False)
    _add_arg('-obj', '--obj_func',
             help='Objective function to use in determining which learner/set'
                  ' of parameters resulted in the best performance.',
             choices=OBJ_FUNC_ABBRS_DICT.keys(),
             default='qwk')
    _add_arg('-baseline', '--evaluate_majority_baseline',
             help='Evaluate the majority baseline model.',
             action='store_true',
             default=False)
    _add_arg('-save_best', '--save_best_features',
             help='Get the best features from each model and write them out '
                  'to files.',
             action='store_true',
             default=False)
    _add_arg('-dbhost', '--mongodb_host',
             help='Host that the MongoDB server is running on.',
             type=str,
             default='localhost')
    _add_arg('-dbport', '--mongodb_port',
             help='Port that the MongoDB server is running on.',
             type=int,
             default=37017)
    _add_arg('-log', '--log_file_path',
             help='Path to log file. If no path is specified, then a "logs" '
                  'directory will be created within the directory specified '
                  'via the --output_dir argument and a log will automatically '
                  'be stored.',
             type=str,
             required=False)
    args = parser.parse_args()

    # Command-line arguments and flags
    games = parse_games_string(args.games)
    training_rounds = args.training_rounds
    max_training_samples_per_round = args.max_training_samples_per_round
    grid_search_folds = args.grid_search_folds
    max_grid_search_samples_per_fold = args.max_grid_search_samples_per_fold
    prediction_label = args.prediction_label
    non_nlp_features = parse_non_nlp_features_string(args.non_nlp_features,
                                                     prediction_label)
    only_non_nlp_features = args.only_non_nlp_features
    nbins = args.nbins
    bin_factor = args.bin_factor
    lognormal = args.lognormal
    power_transform = args.power_transform
    feature_hashing = args.use_feature_hasher
    rescale_predictions = args.rescale_predictions
    data_sampling = args.data_sampling
    learners = parse_learners_string(args.learners)
    host = args.mongodb_host
    port = args.mongodb_port
    obj_func = args.obj_func
    evaluate_majority_baseline = args.evaluate_majority_baseline
    save_best_features = args.save_best_features

    # Validate the input arguments
    if isfile(realpath(args.output_dir)):
        raise FileExistsError('The specified output destination is the name '
                              'of a currently existing file.')
    else:
        output_dir = realpath(args.output_dir)
        
    if save_best_features:
        if learners.issubset(RunCVExperiments._no_introspection_learners):
            loginfo('The specified set of learners do not work with the '
                    'current way of extracting features from models and, '
                    'thus, -save_best/--save_best_features, will be ignored.')
            save_best_features = False
        if feature_hashing:
            raise ValueError('The --save_best_features/-save_best option '
                             'cannot be used in conjunction with the '
                             '--use_feature_hasher/-feature_hasher option.')
    if args.log_file_path:
        if isdir(realpath(args.log_file_path)):
            raise FileExistsError('The specified log file path is the name of'
                                  ' a currently existing directory.')
        else:
            log_file_path = realpath(args.log_file_path)
    else:
        log_file_path = join(output_dir, 'logs', 'learn.log')
    log_dir = dirname(log_file_path)
    if lognormal and power_transform:
        raise ValueError('Both "lognormal" and "power_transform" were '
                         'specified simultaneously.')

    # Output results files to output directory
    makedirs(output_dir, exist_ok=True)
    makedirs(log_dir, exist_ok=True)

    # Set up file handler
    file_handler = logging.FileHandler(log_file_path)
    file_handler.setLevel(logging_debug)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Log a bunch of job attributes
    loginfo('Output directory: {0}'.format(output_dir))
    loginfo('Game{0} to train/evaluate models on: {1}'
            .format('s' if len(games) > 1 else '',
                    ', '.join(games) if VALID_GAMES.difference(games)
                    else 'all games'))
    loginfo('Maximum number of learning rounds to conduct: {0}'
            .format(training_rounds))
    loginfo('Maximum number of training samples to use in each round: {0}'
            .format(max_training_samples_per_round))
    loginfo('Maximum number of grid search folds to use during the grid search'
            ' round: {0}'.format(grid_search_folds))
    loginfo('Maximum number of training samples to use in each grid search '
            'fold: {0}'.format(max_grid_search_samples_per_fold))
    loginfo('Prediction label: {0}'.format(prediction_label))
    loginfo('Data sampling method: {0}'.format(data_sampling))
    loginfo('Lognormal transformation: {0}'.format(lognormal))
    loginfo('Power transformation: {0}'.format(power_transform))
    loginfo('Non-NLP features to use: {0}'
            .format(', '.join(non_nlp_features) if non_nlp_features else 'none'))
    if only_non_nlp_features:
        if not non_nlp_features:
            raise ValueError('No features to train a model on since the '
                             '--only_non_nlp_features flag was used and the '
                             'set of non-NLP features is empty.')
        loginfo('Leaving out all NLP features')
    if nbins == 0:
        if bin_factor:
            raise ValueError('--bin_factor should not be specified if --nbins'
                             ' is not specified or set to 0.')
        bin_ranges = None
    else:
        if bin_factor and bin_factor <= 0:
            raise ValueError('--bin_factor should be set to a positive, '
                             'non-zero value.')
        elif not bin_factor:
            bin_factor = 1.0
        loginfo('Number of bins to split up the distribution of prediction '
                'label values into: {}'.format(nbins))
        loginfo("Factor by which to multiply each succeeding bin's size: {}"
                .format(bin_factor))
    if feature_hashing:
        loginfo('Using feature hashing to increase memory efficiency')
    if rescale_predictions:
        loginfo('Rescaling predicted values based on the mean/standard '
                'deviation of the input values.')
    loginfo('Learners: {0}'.format(', '.join([LEARNER_ABBRS_DICT[learner]
                                              for learner in learners])))
    loginfo('Using {0} as the objective function'.format(obj_func))

    # Connect to running Mongo server
    loginfo('MongoDB host: {0}'.format(host))
    loginfo('MongoDB port: {0}'.format(port))
    try:
        db = connect_to_db(host=host, port=port)
    except ConnectionFailure as e:
        logerr('Unable to connect to MongoDB reviews collection.')
        logerr(e)
        raise e

    # Check to see if the database has the proper index and, if not,
    # index the database here
    index_name = 'steam_id_number_1'
    if not index_name in db.index_information():
        logdebug('Creating index on the "steam_id_number" key...')
        db.create_index('steam_id_number', ASCENDING)

    if nbins:
        # Get ranges of prediction label distribution bins given the
        # number of bins and the factor by which they should be
        # multiplied as the index increases
        bin_ranges = get_bin_ranges_helper(db,
                                           games,
                                           prediction_label,
                                           nbins,
                                           bin_factor,
                                           lognormal=lognormal,
                                           power_transform=power_transform)
        if lognormal or power_transform:
            transformation = ('lognormal' if lognormal
                              else 'x**{0}'.format(power_transform))
        else:
            transformation = None
        loginfo('Bin ranges (nbins = {0}, bin_factor = {1}{2}): {3}'
                .format(nbins,
                        bin_factor,
                        ', {0} transformation'.format(transformation)
                        if transformation
                        else '',
                        bin_ranges))

    # Do learning experiments
    loginfo('Starting incremental learning experiments...')
    learners = sorted(learners)
    try:
        cfg = CVConfig(
                  db=db,
                  games=games,
                  learners=learners,
                  param_grids=[find_default_param_grid(learner)
                               for learner in learners],
                  training_rounds=training_rounds,
                  training_samples_per_round=max_training_samples_per_round,
                  grid_search_samples_per_fold=max_grid_search_samples_per_fold,
                  non_nlp_features=non_nlp_features,
                  prediction_label=prediction_label,
                  objective=obj_func,
                  data_sampling=data_sampling,
                  grid_search_folds=grid_search_folds,
                  hashed_features=0 if feature_hashing else None,
                  nlp_features=not only_non_nlp_features,
                  bin_ranges=bin_ranges,
                  lognormal=lognormal,
                  power_transform=power_transform,
                  majority_baseline=evaluate_majority_baseline,
                  rescale=rescale_predictions)
        experiments = RunCVExperiments(cfg)
    except ValueError as e:
        logerr('Encountered a ValueError while instantiating the CVConfig or '
               'RunCVExperiments instances: {0}'.format(e))
        raise e

    # Generate evaluation report for the majority baseline model, if
    # specified
    if evaluate_majority_baseline:
        loginfo('Generating report for the majority baseline model...')
        loginfo('Majority label: {0}'.format(experiments.majority_label))
        experiments.generate_majority_baseline_report(output_dir)

    # Save the best-performing features
    if save_best_features:
        loginfo('Generating feature coefficient output files for each model '
                '(after all learning rounds)...')
        model_weights_dir = join(output_dir, 'model_weights')
        makedirs(model_weights_dir, exist_ok=True)
        experiments.store_sorted_features(model_weights_dir)

    loginfo('Complete.')


if __name__ == '__main__':
    main()
