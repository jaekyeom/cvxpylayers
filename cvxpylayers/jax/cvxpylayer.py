import diffcp
import time
import cvxpy as cp
from cvxpy.reductions.solvers.conic_solvers.scs_conif import \
    dims_to_solver_dict
import numpy as np
import multiprocessing as mp
from multiprocessing.pool import ThreadPool

try:
    import jax
    from jax import core
    from jax import ad
except ImportError:
    raise ImportError("Unable to import jax/jaxlib. Please follow instructions "
                      "at https://github.com/google/jax.")

class CvxpyLayer(object):
    """A differentiable convex optimization layer

    A CvxpyLayer solves a parametrized convex optimization problem given by a
    CVXPY problem. It solves the problem in its forward pass, and it computes
    the derivative of problem's solution map with respect to the parameters in
    its backward pass. The CVPXY problem must be a disciplined parametrized
    program

    Example usage:
    XXX
    """

    def __init__(self, problem, parameters, variables):
        """Construct a CvxpyLayer

        Args:
          problem: The CVXPY problem; must be DPP.
          parameters: A list of CVXPY Parameters in the problem; the order
                      of the Parameters determines the order in which parameter
                      values must be supplied in the forward pass. Must include
                      every parameter involved in problem.
          variables: A list of CVXPY Variables in the problem; the order of the
                     Variables determines the order of the optimal variable
                     values returned from the forward pass.
        """
        if not problem.is_dpp():
            raise ValueError('Problem must be DPP.')
        if not set(problem.parameters()) == set(parameters):
            raise ValueError("The layer's parameters must exactly match "
                             "problem.parameters")
        if not set(variables).issubset(set(problem.variables())):
            raise ValueError("Argument variables must be a subset of "
                             "problem.variables")

        self.param_order = parameters
        self.param_ids = [p.id for p in self.param_order]
        self.variables = variables
        self.var_dict = {v.id for v in self.variables}

        # Construct compiler
        data, _, _ = problem.get_problem_data(solver=cp.SCS)
        self.compiler = data[cp.settings.PARAM_PROB]
        self.cone_dims = dims_to_solver_dict(data["dims"])

        self.layer_p = core.Primitive("layer")
        self.layer = lambda *params: self.layer_p.bind(*params)

        def layer_impl(*params, solver_args={}):
            """Solve problem (or a batch of problems) corresponding to `params`.

            Args:
            params: a sequence of torch Tensors; the n-th Tensor specifies
                    the value for the n-th CVXPY Parameter. These Tensors
                    can be batched: if a Tensor has 3 dimensions, then its
                    first dimension is interpreted as the batch size. These
                    Tensors must all have the same dtype and device.
            solver_args: a dict of optional arguments, to send to `diffcp`. Keys
                        should be the names of keyword arguments
            """

        def layer_jvp():

        def layer_vjp(args):

        self.layer_p.def_impl(layer_impl)
        ad.defjvp(self.layer_p, layer_jvp)
        ad.defvjp(self.layer_p, layer_vjp)

        if len(params) != len(self.param_ids):
            raise ValueError('A tensor must be provided for each CVXPY '
                             'parameter; received %d tensors, expected %d' % (
                                 len(params), len(self.param_ids)))
        info = {}

        n_jobs_forward = solver_args.get('n_jobs_forward', -1)
        n_jobs_backward = solver_args.get('n_jobs_backward', -1)
        if n_jobs_forward == -1:
            n_jobs_forward = mp.cpu_count()
        if n_jobs_backward == -1:
            n_jobs_backward = mp.cpu_count()

        if n_jobs_forward != 1:
            if hasattr(self, "pool_forward"):
                forward_threads = len(self.pool_forward._pool)
                if forward_threads != n_jobs_forward:
                    self.pool_forward.close()
                    self.pool_forward = ThreadPool(n_jobs_forward)
            else:
                self.pool_forward = ThreadPool(n_jobs_forward)
            solver_args["pool_forward"] = self.pool_forward

        if n_jobs_backward != 1:
            if hasattr(self, "pool_backward"):
                backward_threads = len(self.pool_backward._pool)
                if backward_threads != n_jobs_backward:
                    self.pool_backward.close()
                    self.pool_backward = ThreadPool(n_jobs_backward)
            else:
                self.pool_backward = ThreadPool(n_jobs_backward)
            solver_args["pool_backward"] = self.pool_backward

        f = _CvxpyLayerFn(
            param_order=self.param_order,
            param_ids=self.param_ids,
            variables=self.variables,
            var_dict=self.var_dict,
            compiler=self.compiler,
            cone_dims=self.cone_dims,
            solver_args=solver_args,
            info=info,
        )
        sol = f(*params)
        self.info = info
        return sol


def to_numpy(x):
    # convert torch tensor to numpy array
    return x.cpu().detach().double().numpy()


def to_torch(x, dtype, device):
    # convert numpy array to torch tensor
    return torch.from_numpy(x).type(dtype).to(device)


def _CvxpyLayerFn(
        param_order,
        param_ids,
        variables,
        var_dict,
        compiler,
        cone_dims,
        solver_args,
        info):
    class _CvxpyLayerFnFn(torch.autograd.Function):
        @staticmethod
        def forward(ctx, *params):
            params_numpy = [to_numpy(p) for p in params]

            # infer dtype, device, and whether or not params are batched
            ctx.dtype = params[0].dtype
            ctx.device = params[0].device

            ctx.batch_sizes = []
            for i, (p, q) in enumerate(zip(params, param_order)):
                # check dtype, device of params
                if p.dtype != ctx.dtype:
                    raise ValueError(
                        "Two or more parameters have different dtypes. "
                        "Expected parameter %d to have dtype %s but "
                        "got dtype %s." %
                        (i, str(ctx.dtype), str(p.dtype))
                    )
                if p.device != ctx.device:
                    raise ValueError(
                        "Two or more parameters are on different devices. "
                        "Expected parameter %d to be on device %s "
                        "but got device %s." %
                        (i, str(ctx.device), str(p.device))
                    )

                # check and extract the batch size for the parameter
                # 0 means there is no batch dimension for this parameter
                # and we assume the batch dimension is non-zero
                if p.ndimension() == q.ndim:
                    batch_size = 0
                elif p.ndimension() == q.ndim + 1:
                    batch_size = p.size(0)
                    if batch_size == 0:
                        raise ValueError(
                            "The batch dimension for parameter {} is zero "
                            "but should be non-zero.".format(i))
                else:
                    raise ValueError(
                        "Invalid parameter size passed in. Expected "
                        "parameter {} to have have {} or {} dimensions "
                        "but got {} dimensions".format(
                            i, q.ndim, q.ndim + 1, p.ndimension()))

                ctx.batch_sizes.append(batch_size)

                # validate the parameter shape
                p_shape = p.shape if batch_size == 0 else p.shape[1:]
                if not np.all(p_shape == param_order[i].shape):
                    raise ValueError(
                        "Inconsistent parameter shapes passed in. "
                        "Expected parameter {} to have non-batched shape of "
                        "{} but got {}.".format(
                                i,
                                q.shape,
                                p.shape))

            ctx.batch_sizes = np.array(ctx.batch_sizes)
            ctx.batch = np.any(ctx.batch_sizes > 0)

            if ctx.batch:
                nonzero_batch_sizes = ctx.batch_sizes[ctx.batch_sizes > 0]
                ctx.batch_size = nonzero_batch_sizes[0]
                if np.any(nonzero_batch_sizes != ctx.batch_size):
                    raise ValueError(
                        "Inconsistent batch sizes passed in. Expected "
                        "parameters to have no batch size or all the same "
                        "batch size but got sizes: {}.".format(
                            ctx.batch_sizes))
            else:
                ctx.batch_size = 1

            # canonicalize problem
            start = time.time()
            As, bs, cs, cone_dicts, ctx.shapes = [], [], [], [], []
            for i in range(ctx.batch_size):
                params_numpy_i = [
                    p if sz == 0 else p[i]
                    for p, sz in zip(params_numpy, ctx.batch_sizes)]
                c, _, neg_A, b = compiler.apply_parameters(
                    dict(zip(param_ids, params_numpy_i)),
                    keep_zeros=True)
                A = -neg_A  # cvxpy canonicalizes -A
                As.append(A)
                bs.append(b)
                cs.append(c)
                cone_dicts.append(cone_dims)
                ctx.shapes.append(A.shape)
            info['canon_time'] = time.time() - start

            # compute solution and derivative function
            start = time.time()
            try:
                xs, _, _, _, ctx.DT_batch = diffcp.solve_and_derivative_batch(
                    As, bs, cs, cone_dicts, **solver_args)
            except diffcp.SolverError as e:
                print(
                    "Please consider re-formulating your problem so that "
                    "it is always solvable.")
                raise e
            info['solve_time'] = time.time() - start

            # extract solutions and append along batch dimension
            sol = [[] for _ in range(len(variables))]
            for i in range(ctx.batch_size):
                sltn_dict = compiler.split_solution(
                    xs[i], active_vars=var_dict)
                for j, v in enumerate(variables):
                    sol[j].append(to_torch(
                        sltn_dict[v.id], ctx.dtype, ctx.device).unsqueeze(0))
            sol = [torch.cat(s, 0) for s in sol]

            if not ctx.batch:
                sol = [s.squeeze(0) for s in sol]

            return tuple(sol)

        @staticmethod
        def backward(ctx, *dvars):
            dvars_numpy = [to_numpy(dvar) for dvar in dvars]

            if not ctx.batch:
                dvars_numpy = [np.expand_dims(dvar, 0) for dvar in dvars_numpy]

            # differentiate from cvxpy variables to cone problem data
            dxs, dys, dss = [], [], []
            for i in range(ctx.batch_size):
                del_vars = {}
                for v, dv in zip(variables, [dv[i] for dv in dvars_numpy]):
                    del_vars[v.id] = dv
                dxs.append(compiler.split_adjoint(del_vars))
                dys.append(np.zeros(ctx.shapes[i][0]))
                dss.append(np.zeros(ctx.shapes[i][0]))

            dAs, dbs, dcs = ctx.DT_batch(dxs, dys, dss)

            # differentiate from cone problem data to cvxpy parameters
            start = time.time()
            grad = [[] for _ in range(len(param_order))]
            for i in range(ctx.batch_size):
                del_param_dict = compiler.apply_param_jac(
                    dcs[i], -dAs[i], dbs[i])
                for j, p in enumerate(param_order):
                    grad[j] += [to_torch(del_param_dict[p.id],
                                         ctx.dtype, ctx.device).unsqueeze(0)]
            grad = [torch.cat(g, 0) for g in grad]
            info['dcanon_time'] = time.time() - start

            if not ctx.batch:
                grad = [g.squeeze(0) for g in grad]
            else:
                for i, sz in enumerate(ctx.batch_sizes):
                    if sz == 0:
                        grad[i] = grad[i].sum(dim=0)

            return tuple(grad)

    return _CvxpyLayerFnFn.apply
