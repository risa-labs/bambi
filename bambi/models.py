# pylint: disable=no-name-in-module
# pylint: disable=too-many-lines
import logging
import warnings

from copy import deepcopy

import pymc as pm
import pandas as pd

from arviz.plots import plot_posterior

from formulae import design_matrices

from bambi.backend import PyMCModel
from bambi.defaults import get_builtin_family
from bambi.model_components import ConstantComponent, DistributionalComponent
from bambi.families import Family, univariate
from bambi.formula import Formula
from bambi.priors import Prior, PriorScaler
from bambi.utils import (
    clean_formula_lhs,
    extra_namespace,
    get_aliased_name,
    get_auxiliary_parameters,
    listify,
    indentify,
    wrapify,
)
from bambi.version import __version__

_log = logging.getLogger("bambi")


class Model:
    """Specification of model class.

    Parameters
    ----------
    formula : str or bambi.formula.Formula
        A model description written using the formula syntax from the ``formulae`` library.
    data : pandas.DataFrame
        A pandas dataframe containing the data on which the model will be fit, with column
        names matching variables defined in the formula.
    family : str or bambi.families.Family
        A specification of the model family (analogous to the family object in R). Either
        a string, or an instance of class ``bambi.families.Family``. If a string is passed, a
        family with the corresponding name must be defined in the defaults loaded at ``Model``
        initialization. Valid pre-defined families are ``"bernoulli"``, ``"beta"``,
        ``"binomial"``, ``"categorical"``, ``"gamma"``, ``"gaussian"``, ``"negativebinomial"``,
        ``"poisson"``, ``"t"``, and ``"wald"``. Defaults to ``"gaussian"``.
    priors : dict
        Optional specification of priors for one or more terms. A dictionary where the keys are
        the names of terms in the model, "common," or "group_specific" and the values are
        instances of class ``Prior``. If priors are unset, uses automatic priors inspired by
        the R rstanarm library.
    link : str or Dict[str, str]
        The name of the link function to use. Valid names are ``"cloglog"``, ``"identity"``,
        ``"inverse_squared"``, ``"inverse"``, ``"log"``, ``"logit"``, ``"probit"``, and
        ``"softmax"``. Not all the link functions can be used with all the families.
        If a dictionary, keys are the names of the target parameters and the values are the names
        of the link functions.
    categorical : str or list
        The names of any variables to treat as categorical. Can be either a single variable
        name, or a list of names. If categorical is ``None``, the data type of the columns in
        the ``data`` will be used to infer handling. In cases where numeric columns are
        to be treated as categorical (e.g., group specific factors coded as numerical IDs),
        explicitly passing variable names via this argument is recommended.
    potentials : A list of 2-tuples.
        Optional specification of potentials. A potential is an arbitrary expression added to the
        likelihood, this is generally useful to add constrains to models, that are difficult to
        express otherwise. The first term of a 2-tuple is the name of a variable in the model, the
        second a lambda function expressing the desired constraint.
        If a constraint involves n variables, you can pass n 2-tuples or pass a tuple which first
        element is a n-tuple and second element is a lambda function with n arguments. The number
        and order of the lambda function has to match the number and order of the variables names.
    dropna : bool
        When ``True``, rows with any missing values in either the predictors or outcome are
        automatically dropped from the dataset in a listwise manner.
    auto_scale : bool
        If ``True`` (default), priors are automatically rescaled to the data
        (to be weakly informative) any time default priors are used. Note that any priors
        explicitly set by the user will always take precedence over default priors.
    noncentered : bool
        If ``True`` (default), uses a non-centered parameterization for normal hyperpriors on
        grouped parameters. If ``False``, naive (centered) parameterization is used.
    """

    # pylint: disable=too-many-instance-attributes
    def __init__(
        self,
        formula,
        data,
        family="gaussian",
        priors=None,
        link=None,
        categorical=None,
        potentials=None,
        dropna=False,
        auto_scale=True,
        noncentered=True,
    ):
        # attributes that are set later
        self.components = {}  # Constant and Distributional components
        self.built = False  # build()

        # build() will loop over this, calling _set_priors()
        self._added_priors = {}

        self.family = None  # _add_response()
        self.backend = None  # _set_backend()

        self.auto_scale = auto_scale
        self.dropna = dropna
        self.formula = formula
        self.noncentered = noncentered
        self.potentials = potentials

        # Read and clean data
        if not isinstance(data, pd.DataFrame):
            raise ValueError("'data' must be a pandas DataFrame.")

        # Some columns are converted to categorical
        self.data = with_categorical_cols(data, categorical)

        # Handle priors
        priors = {} if priors is None else deepcopy(priors)

        # Obtain design matrices and related objects.
        na_action = "drop" if dropna else "error"

        # Create family
        self._set_family(family, link)

        ## Main component
        design = design_matrices(self.formula.main, self.data, na_action, 1, extra_namespace)
        if design.response is None:
            raise ValueError(
                "No outcome variable is set! "
                "Please specify an outcome variable using the formula interface."
            )

        # This response_name allows to grab the response component from the `.components` dict
        self.response_name = design.response.name
        if self.response_name in priors:
            response_prior = priors[self.response_name]
        else:
            response_prior = priors

        self.components[self.response_name] = DistributionalComponent(
            design, response_prior, self.response_name, "data", self
        )

        # Get auxiliary parameters, so we add either distributional components or constant ones
        auxiliary_parameters = list(get_auxiliary_parameters(self.family))

        ## Other components
        ### Distributional
        for name, extra_formula in zip(self.formula.additionals_lhs, self.formula.additionals):
            # Check 'name' is part of parameter values
            if name not in auxiliary_parameters:
                raise ValueError(
                    f"'{name}' is not a parameter of the family."
                    f"Available parameters: {auxiliary_parameters}."
                )

            # Create design matrix, only for the response part
            design = design_matrices(
                clean_formula_lhs(extra_formula), self.data, na_action, 1, extra_namespace
            )

            # If priors were not passed, pass an empty dictionary
            component_prior = priors.get(name, {})

            # Create distributional component
            self.components[name] = DistributionalComponent(
                design, component_prior, name, "parameter", self
            )

            # Remove parameter name from the list
            auxiliary_parameters.remove(name)

        ### Constant
        for name in auxiliary_parameters:
            component_prior = priors.get(name, None)
            self.components[name] = ConstantComponent(
                name, component_prior, self.response_name, self
            )

        # Build priors
        self._build_priors()

    def fit(
        self,
        draws=1000,
        tune=1000,
        discard_tuned_samples=True,
        omit_offsets=True,
        include_mean=False,
        inference_method="mcmc",
        init="auto",
        n_init=50000,
        chains=None,
        cores=None,
        random_seed=None,
        **kwargs,
    ):
        """Fit the model using PyMC.

        Parameters
        ----------
        draws: int
            The number of samples to draw from the posterior distribution. Defaults to 1000.
        tune : int
            Number of iterations to tune. Defaults to 1000. Samplers adjust the step sizes,
            scalings or similar during tuning. These tuning samples are be drawn in addition to the
            number specified in the ``draws`` argument, and will be discarded unless
            ``discard_tuned_samples`` is set to ``False``.
        discard_tuned_samples : bool
            Whether to discard posterior samples of the tune interval. Defaults to ``True``.
        omit_offsets : bool
            Omits offset terms in the ``InferenceData`` object returned when the model includes
            group specific effects. Defaults to ``True``.
        include_mean : bool
            Compute the posterior of the mean response. Defaults to ``False``.
        inference_method : str
            The method to use for fitting the model. By default, ``"mcmc"``. This automatically
            assigns a MCMC method best suited for each kind of variables, like NUTS for continuous
            variables and Metropolis for non-binary discrete ones. Alternatively, ``"vi"``, in
            which case the model will be fitted using variational inference as implemented in PyMC
            using the ``fit`` function.
            Finally, ``"laplace"``, in which case a Laplace approximation is used and is not
            recommended other than for pedagogical use.
            To use the PyMC numpyro and blackjax samplers, use ``nuts_numpyro`` or ``nuts_blackjax``
            respectively. Both methods will only work if you can use NUTS sampling, so your model
            must be differentiable.
        init : str
            Initialization method. Defaults to ``"auto"``. The available methods are:
            * auto: Use ``"jitter+adapt_diag"`` and if this method fails it uses ``"adapt_diag"``.
            * adapt_diag: Start with a identity mass matrix and then adapt a diagonal based on the
            variance of the tuning samples. All chains use the test value (usually the prior mean)
            as starting point.
            * jitter+adapt_diag: Same as ``"adapt_diag"``, but use test value plus a uniform jitter
            in [-1, 1] as starting point in each chain.
            * advi+adapt_diag: Run ADVI and then adapt the resulting diagonal mass matrix based on
            the sample variance of the tuning samples.
            * advi+adapt_diag_grad: Run ADVI and then adapt the resulting diagonal mass matrix based
            on the variance of the gradients during tuning. This is **experimental** and might be
            removed in a future release.
            * advi: Run ADVI to estimate posterior mean and diagonal mass matrix.
            * advi_map: Initialize ADVI with MAP and use MAP as starting point.
            * map: Use the MAP as starting point. This is strongly discouraged.
            * adapt_full: Adapt a dense mass matrix using the sample covariances. All chains use the
            test value (usually the prior mean) as starting point.
            * jitter+adapt_full: Same as ``"adapt_full"``, but use test value plus a uniform jitter
            in [-1, 1] as starting point in each chain.
        n_init : int
            Number of initialization iterations. Only works for ``"advi"`` init methods.
        chains : int
            The number of chains to sample. Running independent chains is important for some
            convergence statistics and can also reveal multiple modes in the posterior. If ``None``,
            then set to either ``cores`` or 2, whichever is larger.
        cores : int
            The number of chains to run in parallel. If ``None``, it is equal to the number of CPUs
            in the system unless there are more than 4 CPUs, in which case it is set to 4.
        random_seed : int or list of ints
            A list is accepted if cores is greater than one.
        **kwargs :
            For other kwargs see the documentation for ``PyMC.sample()``.

        Returns
        -------
        An ArviZ ``InferenceData`` instance if inference_method is  ``"mcmc"`` (default),
        "nuts_numpyro", "nuts_blackjax" or "laplace".
        An ``Approximation`` object if  ``"vi"``.
        """
        method = kwargs.pop("method", None)
        if method is not None:
            if inference_method == "vi":
                kwargs["method"] = method
            else:
                warnings.warn(
                    "the method argument has been deprecated, please use inference_method",
                    FutureWarning,
                )
                inference_method = method

        if not self.built:
            self.build()

        # Tell user which event is being modeled
        if isinstance(self.family, univariate.Bernoulli):
            response = self.components[self.response_name]
            _log.info(
                "Modeling the probability that %s==%s",
                response.response_term.name,
                str(response.response_term.success),
            )

        return self.backend.run(
            draws=draws,
            tune=tune,
            discard_tuned_samples=discard_tuned_samples,
            omit_offsets=omit_offsets,
            include_mean=include_mean,
            inference_method=inference_method,
            init=init,
            n_init=n_init,
            chains=chains,
            cores=cores,
            random_seed=random_seed,
            **kwargs,
        )

    def build(self):
        """Set up the model for sampling/fitting.

        Creates an instance of the underlying PyMC model and adds all the necessary terms to it.

        Returns
        -------
        None
        """
        self.backend = PyMCModel()
        self.backend.build(self)
        self.built = True

    def set_priors(self, priors=None, common=None, group_specific=None):
        """Set priors for one or more existing terms.

        Parameters
        ----------
        priors : dict
            Dictionary of priors to update. Keys are names of terms to update; values are the new
            priors (either a ``Prior`` instance, or an int or float that scales the default priors).
        common : Prior, int, or float
            A prior specification to apply to all common terms included in the model.
        group_specific : Prior, int, or float
            A prior specification to apply to all group specific terms included in the model.

        Returns
        -------
        None
        """
        kwargs = dict(zip(["priors", "common", "group_specific"], [priors, common, group_specific]))
        self._added_priors.update(kwargs)
        self._build_priors()  # After updating, we need to rebuild priors.
        self.built = False

    def _build_priors(self):
        """Carry out all operations related to the construction and/or scaling of priors."""
        # Set custom priors that have been passed via `Model.set_priors()`
        self._set_priors(**self._added_priors)

        # Prepare all priors
        for component in self.distributional_components.values():
            component.build_priors()

        for name, component in self.constant_components.items():
            if isinstance(component.prior, Prior):
                component.prior.auto_scale = False
            elif isinstance(component.prior, (int, float)):
                continue
            elif component.prior is not None:
                raise ValueError(f"'{component.prior}' is not a valid prior.")
            else:
                default_prior = self.family.default_priors.get(name, None)
                if default_prior is None:
                    raise ValueError(f"The component '{name}' needs a prior.")
                component.prior = default_prior

        # Scale priors if there is at least one term in the model and auto_scale is True
        if self.auto_scale:
            self.scaler = PriorScaler(self)
            self.scaler.scale()

    def _set_priors(self, priors=None, common=None, group_specific=None):
        """Internal version of ``set_priors()``, with same arguments.

        Runs during ``Model._build_priors()``.
        """
        # 'common' and 'group_specific' only apply to the response component
        if common is not None:
            for term in self.response_component.common_terms.values():
                term.prior = common

        if group_specific is not None:
            for term in self.response_component.group_specific_terms.values():
                term.prior = group_specific

        if priors is not None:
            priors = deepcopy(priors)

            # The only distributional component is the response term
            if len(self.distributional_components) == 1:
                for name, component in self.constant_components.items():
                    prior = priors.pop(name) if name in priors else None
                    if prior:
                        component.update_priors(prior)
                # Pass all the other priors to the response component
                self.response_component.update_priors(priors)
            # There are more than one distributional components.
            else:
                for name, component in self.components.items():
                    prior = priors.get(name)
                    if prior:
                        component.update_priors(prior)

    def _set_family(self, family, link):
        """Set the Family of the model.

        Parameters
        ----------
        family : str or bambi.families.Family
            A specification of the model family.
            Either a string, or an instance of class ``families.Family``.
            If a string is passed, a family with the corresponding name must be defined in the
            defaults loaded at model initialization.
        link : str or Dict[str, str]
            The name of the link function to use. Valid names are ``"cloglog"``, ``"identity"``,
            ``"inverse_squared"``, ``"inverse"``, ``"log"``, ``"logit"``, ``"probit"``, and
            ``"softmax"``. Not all the link functions can be used with all the families.
            If a dictionary, keys are the names of the target parameters and the values are the
            names of the link functions.

        Returns
        -------
        None
        """

        # If string, get builtin family
        if isinstance(family, str):
            family = get_builtin_family(family)

        # Always ensure family is indeed instance of Family
        if not isinstance(family, Family):
            raise ValueError("'family' must be a string or a Family object.")

        # Override family's link if another is explicitly passed
        # If `link` is string, we assume it wants to override only the `parent` parameter
        if link is not None:
            if isinstance(link, str):
                links = family.link.copy()
                links[family.likelihood.parent] = link
            elif isinstance(link, dict):
                links = link
            else:
                raise ValueError("'link' must be of type 'str' or 'dict'.")
            family.link = links

        self.family = family

    def set_alias(self, aliases):
        """Set aliases for the terms and auxiliary parameters in the model

        Parameters
        ----------
        aliases : dict
            A dictionary where key represents the original term name and the value is the alias.

        Returns
        -------
        None
        """
        if not isinstance(aliases, dict):
            raise ValueError(f"'aliases' must be a dictionary, not a {type(aliases)}.")

        response_name = get_aliased_name(self.response_component.response_term)

        # If there is a single distributional component (the response)
        #   * Keys are the names of the terms and the values are their aliases.
        # If there are multiple distributional components
        #   * Keys are the name of the components responses
        #     * If it's a constant component, the value must be a string
        #     * If it's a distributional component, the value must be a dictionary
        #        * Here, names are term names, and values are their aliases
        #     * There's unavoidable redundancy in the response name
        #       {"y": {"y": "alias"}, "sigma": {"sigma": "alias"}}
        if len(self.distributional_components) == 1:  # pylint: disable=too-many-nested-blocks
            for name, alias in aliases.items():
                assert isinstance(alias, str)
                if name in self.response_component.terms:
                    self.response_component.terms[name].alias = alias

                if name in self.constant_components:
                    self.constant_components[name].alias = alias

                if name == response_name:
                    self.response_component.response_term.alias = alias

                # Now add aliases for hyperpriors in group specific terms
                for term in self.response_component.group_specific_terms.values():
                    if name in term.prior.args:
                        term.hyperprior_alias = {name: alias}
        else:
            for component_name, component_aliases in aliases.items():
                if component_name in self.constant_components:
                    assert isinstance(component_aliases, str)
                    self.constant_components[component_name].alias = component_aliases
                else:
                    assert isinstance(component_aliases, dict)
                    assert component_name in self.distributional_components
                    component = self.distributional_components[component_name]
                    for name, alias in component_aliases.items():
                        if name in component.terms:
                            component.terms[name].alias = alias

                        # Useful for non-response distributional components
                        if name == component.response_name:
                            component.alias = alias

                        for term in component.group_specific_terms.values():
                            if name in term.prior.args:
                                term.hyperprior_alias = {name: alias}

        # Model needs to be rebuilt after modifying aliases
        self.built = False

    def _check_built(self):
        # Checks if model is built, raises ValueError if not
        if not self.built:
            raise ValueError(
                "Model is not built yet! "
                "Call .build() to build the model or .fit() to build and sample from the posterior."
            )

    def plot_priors(
        self,
        draws=5000,
        var_names=None,
        random_seed=None,
        figsize=None,
        textsize=None,
        hdi_prob=None,
        round_to=2,
        point_estimate="mean",
        kind="kde",
        bins=None,
        omit_offsets=True,
        omit_group_specific=True,
        ax=None,
    ):
        """
        Samples from the prior distribution and plots its marginals.

        Parameters
        ----------
        draws : int
            Number of draws to sample from the prior predictive distribution. Defaults to 5000.
        var_names : str or list
            A list of names of variables for which to compute the posterior predictive
            distribution. Defaults to ``None`` which means to include both observed and
            unobserved RVs.
        random_seed : int
            Seed for the random number generator.
        figsize : tuple
            Figure size. If ``None`` it will be defined automatically.
        textsize : float
            Text size scaling factor for labels, titles and lines. If ``None`` it will be
            autoscaled based on ``figsize``.
        hdi_prob : float or str
            Plots highest density interval for chosen percentage of density.
            Use ``"hide"`` to hide the highest density interval. Defaults to 0.94.
        round_to : int
            Controls formatting of floats. Defaults to 2 or the integer part, whichever is bigger.
        point_estimate : str
            Plot point estimate per variable. Values should be ``"mean"``, ``"median"``, ``"mode"``
            or ``None``. Defaults to ``"auto"`` i.e. it falls back to default set in
            ArviZ's rcParams.
        kind : str
            Type of plot to display (``"kde"`` or ``"hist"``) For discrete variables this argument
            is ignored and a histogram is always used.
        bins : integer or sequence or "auto"
            Controls the number of bins, accepts the same keywords ``matplotlib.pyplot.hist()``
            does. Only works if ``kind == "hist"``. If ``None`` (default) it will use ``"auto"``
            for continuous variables and ``range(xmin, xmax + 1)`` for discrete variables.
        omit_offsets : bool
            Whether to omit offset terms in the plot. Defaults to ``True``.
        omit_group_specific : bool
            Whether to omit group specific effects in the plot. Defaults to ``True``.
        ax : numpy array-like of matplotlib axes or bokeh figures
            A 2D array of locations into which to plot the densities. If not supplied, ArviZ will
            create its own array of plot areas (and return it).
        **kwargs
            Passed as-is to ``matplotlib.pyplot.hist()`` or ``matplotlib.pyplot.plot()`` function
            depending on the value of ``kind``.

        Returns
        -------
        axes: matplotlib axes
        """
        self._check_built()

        unobserved_rvs_names = []
        flat_rvs = []
        for unobserved in self.backend.model.unobserved_RVs:
            if "Flat" in unobserved.__str__():
                flat_rvs.append(unobserved.name)
            else:
                unobserved_rvs_names.append(unobserved.name)
        if var_names is None:
            var_names = pm.util.get_default_varnames(
                unobserved_rvs_names, include_transformed=False
            )
        else:
            flat_rvs = [fv for fv in flat_rvs if fv in var_names]
            var_names = [vn for vn in var_names if vn not in flat_rvs]

        if flat_rvs:
            _log.info(
                "Variables %s have flat priors, and hence they are not plotted", ", ".join(flat_rvs)
            )

        if omit_offsets:
            var_names = [name for name in var_names if not name.endswith("_offset")]

        if omit_group_specific:
            group_specific_var_names = [
                name
                for component in self.distributional_components.values()
                for name in component.group_specific_terms
            ]
            var_names = [name for name in var_names if name not in group_specific_var_names]

        axes = None
        if var_names:
            # Sort variable names so Intercept is in the beginning
            if "Intercept" in var_names:
                var_names.insert(0, var_names.pop(var_names.index("Intercept")))
            pps = self.prior_predictive(draws=draws, var_names=var_names, random_seed=random_seed)

            axes = plot_posterior(
                pps,
                group="prior",
                var_names=var_names,
                figsize=figsize,
                textsize=textsize,
                hdi_prob=hdi_prob,
                round_to=round_to,
                point_estimate=point_estimate,
                kind=kind,
                bins=bins,
                ax=ax,
            )
        return axes

    def prior_predictive(self, draws=500, var_names=None, omit_offsets=True, random_seed=None):
        """Generate samples from the prior predictive distribution.

        Parameters
        ----------
        draws : int
            Number of draws to sample from the prior predictive distribution. Defaults to 500.
        var_names : str or list
            A list of names of variables for which to compute the prior predictive distribution.
            Defaults to ``None`` which means both observed and unobserved RVs.
        omit_offsets : bool
            Whether to omit offset terms in the plot. Defaults to ``True``.
        random_seed : int
            Seed for the random number generator.

        Returns
        -------
        InferenceData
            ``InferenceData`` object with the groups ``prior``, ``prior_predictive`` and
            ``observed_data``.
        """
        self._check_built()

        if var_names is None:
            variables = self.backend.model.unobserved_RVs + self.backend.model.observed_RVs
            variables_names = [v.name for v in variables]
            var_names = pm.util.get_default_varnames(variables_names, include_transformed=False)

        if omit_offsets:
            var_names = [name for name in var_names if not name.endswith("_offset")]

        idata = pm.sample_prior_predictive(
            samples=draws, var_names=var_names, model=self.backend.model, random_seed=random_seed
        )

        for group in idata.groups():
            getattr(idata, group).attrs["modeling_interface"] = "bambi"
            getattr(idata, group).attrs["modeling_interface_version"] = __version__

        return idata

    def predict(self, idata, kind="mean", data=None, inplace=True, include_group_specific=True):
        """Predict method for Bambi models

        Obtains in-sample and out-of-sample predictions from a fitted Bambi model.

        Parameters
        ----------
        idata : InferenceData
            The ``InferenceData`` instance returned by ``.fit()``.
        kind : str
            Indicates the type of prediction required. Can be ``"mean"`` or ``"pps"``. The
            first returns draws from the posterior distribution of the mean, while the latter
            returns the draws from the posterior predictive distribution
            (i.e. the posterior probability distribution for a new observation).
            Defaults to ``"mean"``.
        data : pandas.DataFrame or None
            An optional data frame with values for the predictors that are used to obtain
            out-of-sample predictions. If omitted, the original dataset is used.
        include_group_specific : bool
            If ``True`` make predictions including the group specific effects. Otherwise,
            predictions are made with common effects only (i.e. group specific are set
            to zero).
        inplace : bool
            If ``True`` it will modify ``idata`` in-place. Otherwise, it will return a copy of
            ``idata`` with the predictions added. If ``kind="mean"``, a new variable ending in
            ``"_mean"`` is added to the ``posterior`` group. If ``kind="pps"``, it appends a
            ``posterior_predictive`` group to ``idata``. If any of these already exist, it will be
            overwritten.

        Returns
        -------
        InferenceData or None
        """
        if kind not in ("mean", "pps"):
            raise ValueError("'kind' must be one of 'mean' or 'pps'")

        if not inplace:
            idata = deepcopy(idata)

        response_aliased_name = get_aliased_name(self.response_component.response_term)

        # ALWAYS predict the mean response
        means_dict = {}
        response_dim = response_aliased_name + "_obs"
        for name, component in self.distributional_components.items():
            if name == self.response_name:
                var_name = f"{response_aliased_name}_mean"
            else:
                component_aliased_name = component.alias if component.alias else name
                var_name = f"{response_aliased_name}_{component_aliased_name}"

            means_dict[var_name] = component.predict(idata, data, include_group_specific)

            # Drop var/dim if already present. Needed for out-of-sample predictions.
            if var_name in idata.posterior.data_vars:
                idata.posterior = idata.posterior.drop_vars(var_name)

        if response_dim in idata.posterior.dims:
            idata.posterior = idata.posterior.drop_dims(response_dim)

        # Use the first DataArray to get the number of observations
        obs_n = len(list(means_dict.values())[0].coords.get(response_dim))
        idata.posterior = idata.posterior.assign_coords({response_dim: list(range(obs_n))})

        for name, value in means_dict.items():
            idata.posterior[name] = value

        # Only if requested predict the predictive distribution
        if kind == "pps":
            required_kwargs = {"model": self, "posterior": idata.posterior}
            optional_kwargs = {"data": data}

            pps = self.family.posterior_predictive(**required_kwargs, **optional_kwargs)
            pps = pps.to_dataset(name=response_aliased_name)

            if "posterior_predictive" in idata:
                del idata.posterior_predictive
            idata.add_groups({"posterior_predictive": pps})
            idata.posterior_predictive = idata.posterior_predictive.assign_attrs(
                modeling_interface="bambi", modeling_interface_version=__version__
            )

        if inplace:
            return None
        else:
            return idata

    def graph(self, formatting="plain", name=None, figsize=None, dpi=300, fmt="png"):
        """Produce a graphviz Digraph from a built Bambi model.

        Requires graphviz, which may be installed most easily with
            ``conda install -c conda-forge python-graphviz``

        Alternatively, you may install the ``graphviz`` binaries yourself, and then
        ``pip install graphviz`` to get the python bindings.
        See http://graphviz.readthedocs.io/en/stable/manual.html for more information.

        Parameters
        ----------
        formatting : str
            One of ``"plain"`` or ``"plain_with_params"``. Defaults to ``"plain"``.
        name : str
            Name of the figure to save. Defaults to ``None``, no figure is saved.
        figsize : tuple
            Maximum width and height of figure in inches. Defaults to ``None``, the figure size is
            set automatically. If defined and the drawing is larger than the given size, the drawing
            is uniformly scaled down so that it fits within the given size.  Only works if ``name``
            is not ``None``.
        dpi : int
            Point per inch of the figure to save.
            Defaults to 300. Only works if ``name`` is not ``None``.
        fmt : str
            Format of the figure to save.
            Defaults to ``"png"``. Only works if ``name`` is not ``None``.

        Returns
        -------
        graphviz.Digraph
            The graph

        Example
        --------
        >>> model = Model("y ~ x + (1|z)")
        >>> model.build()
        >>> model.graph()

        >>> model = Model("y ~ x + (1|z)")
        >>> model.fit()
        >>> model.graph()
        """
        self._check_built()

        graphviz = pm.model_to_graphviz(model=self.backend.model, formatting=formatting)

        width, height = (None, None) if figsize is None else figsize

        if name is not None:
            graphviz_ = graphviz.copy()
            graphviz_.graph_attr.update(size=f"{width},{height}!")
            graphviz_.graph_attr.update(dpi=str(dpi))
            graphviz_.render(filename=name, format=fmt, cleanup=True)

        return graphviz

    @property
    def formula(self):
        return self._formula

    @formula.setter
    def formula(self, value):
        if isinstance(value, str):
            self._formula = Formula(value)
        elif isinstance(value, Formula):
            self._formula = value
        else:
            raise ValueError("'.formula' must be instance of 'str' or 'bambi.Formula'")

    def __str__(self):
        # Empty list with the output components
        output_list = []

        # Build header
        parent_name = self.family.likelihood.parent
        formulas = self.formula.get_all_formulas()
        family_name = self.family.name

        links = [
            f"{key} = {value.name}"
            for key, value in self.family.link.items()
            if key == parent_name or key in self.distributional_components
        ]
        observations = self.response_component.response_term.data.shape[0]

        header_dict = {
            "Formula: ": formulas,
            "Family: ": family_name,
            "Link: ": links,
            "Observations: ": str(observations),
            "Priors: ": "",
        }

        width = 16
        spacer = "\n" + " " * width
        for key, value in header_dict.items():
            output_list.append(key.rjust(width) + spacer.join(listify(value)))

        # Build priors section
        priors_dict = {parent_name: make_priors_summary(self.response_component)}

        if self.constant_components:
            aux_str = "\n".join(
                [prior_repr(component) for component in self.constant_components.values()]
            )
            aux_str = "Auxiliary parameters\n" + wrapify(indentify(aux_str, 4), 100, 4)
            priors_dict[parent_name] = priors_dict[parent_name] + "\n\n" + aux_str

        for name, component in self.distributional_components.items():
            if component.response_kind == "data":
                continue
            priors_dict[name] = make_priors_summary(component)

        for key, value in priors_dict.items():
            priors_dict[key] = indentify(value, 4)

        for key, value in priors_dict.items():
            output_list.append(indentify(f"target = {key}" + "\n" + value, 4))

        if self.backend and self.backend.fit:
            foot_list = [
                "------",
                "* To see a plot of the priors call the .plot_priors() method.",
                "* To see a summary or plot of the posterior pass the object returned by .fit() to "
                "az.summary() or az.plot_trace()",
            ]
            output_list.extend(foot_list)

        return "\n".join(output_list)

    def __repr__(self):
        return self.__str__()

    @property
    def response_component(self):
        return self.components[self.response_name]

    @property
    def constant_components(self):
        return {k: v for k, v in self.components.items() if isinstance(v, ConstantComponent)}

    @property
    def distributional_components(self):
        return {k: v for k, v in self.components.items() if isinstance(v, DistributionalComponent)}


def with_categorical_cols(data, columns):
    # Convert 'object' and explicitly asked columns to categorical.
    object_columns = list(data.select_dtypes("object").columns)
    to_convert = list(set(object_columns + listify(columns)))
    if to_convert:
        data = data.copy()  # don't modify original data frame
        data[to_convert] = data[to_convert].apply(lambda x: x.astype("category"))
    return data


def prior_repr(term):
    return f"{term.name} ~ {term.prior}"


def make_priors_summary(component: DistributionalComponent):
    # Common effects
    priors_common = [
        prior_repr(term) for term in component.common_terms.values() if term.kind != "offset"
    ]
    if component.intercept_term:
        priors_common.insert(0, prior_repr(component.intercept_term))

    # Group-specific effects
    priors_group = [prior_repr(term) for term in component.group_specific_terms.values()]

    # Offsets
    offsets = [f"{term.name} ~ 1" for term in component.offset_terms.values()]

    priors_dict = {
        "Common-level effects": priors_common,
        "Group-level effects": priors_group,
        "Offset effects": offsets,
    }

    priors_list = []
    for group, priors in priors_dict.items():
        if priors:
            priors_list.append(group + "\n" + wrapify(indentify("\n".join(priors), 4), 100, 4))

    return "\n\n".join(priors_list)
