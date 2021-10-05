import torch
import numpy as np
import matplotlib.pyplot as plt
import pyro
import pyro.contrib.gp as gp
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader

# from util import make_pinwheel
import seaborn as sns
from warp_gp import WarpGP

device = "cuda" if torch.cuda.is_available() else "cpu"
print("Using {} device".format(device))


# Define model
class VariationalLMCWarpGP(WarpGP):
    # def __init__(self, X, view_idx, n, n_spatial_dims, m_X_per_view, m_G):
    def __init__(
        self,
        data_dict,
        m_X_per_view,
        m_G,
        data_init=True,
        minmax_init=False,
        n_spatial_dims=2,
        n_noise_variance_params=1,
        kernel_func=gp.kernels.RBF,
        n_latent_gps=1,
    ):
        super(VariationalLMCWarpGP, self).__init__(
            data_dict,
            data_init=True,
            n_spatial_dims=2,
            n_noise_variance_params=1,
            kernel_func=gp.kernels.RBF,
        )

        self.m_X_per_view = m_X_per_view
        self.m_G = m_G
        self.n_latent_gps = n_latent_gps

        if data_init:
            # Initialize inducing locations with a subset of the data
            Xtilde = torch.zeros([self.n_views, self.m_X_per_view, self.n_spatial_dims])
            for ii in range(self.n_views):
                curr_X_spatial_list = []
                for mod in self.modality_names:
                    curr_idx = self.view_idx[mod][ii]
                    curr_modality_and_view_spatial = data_dict[mod]["spatial_coords"][
                        curr_idx, :
                    ]
                    curr_X_spatial_list.append(curr_modality_and_view_spatial)
                curr_X_spatial = torch.cat(curr_X_spatial_list, dim=0)
                rand_idx = np.random.choice(
                    np.arange(curr_X_spatial.shape[0]),
                    size=self.m_X_per_view,
                    replace=False,
                )
                Xtilde[ii, :, :] = curr_X_spatial[rand_idx]
            # self.Xtilde = nn.Parameter(Xtilde)
            self.Xtilde = Xtilde.clone()
        elif minmax_init:

            Xtilde = torch.zeros([self.n_views, self.m_X_per_view, self.n_spatial_dims])
            for ii in range(self.n_views):
                curr_X_spatial_list = []
                for mod in self.modality_names:
                    curr_idx = self.view_idx[mod][ii]
                    curr_modality_and_view_spatial = data_dict[mod]["spatial_coords"][
                        curr_idx, :
                    ]
                    curr_X_spatial_list.append(curr_modality_and_view_spatial)
                curr_X_spatial = torch.cat(curr_X_spatial_list, dim=0)
                curr_inducing_locs = torch.linspace(
                    torch.min(curr_X_spatial),
                    torch.max(curr_X_spatial),
                    self.m_X_per_view,
                )
                Xtilde[ii, :, 0] = curr_inducing_locs
                self.Xtilde = Xtilde.clone()
        else:
            # Random initialization of inducing locations
            self.Xtilde = nn.Parameter(
                torch.randn([self.n_views, self.m_X_per_view, self.n_spatial_dims])
            )
        # self.Gtilde = nn.Parameter(10 * torch.randn([self.m_G, self.n_spatial_dims]))
        self.Gtilde = nn.Parameter(
            torch.linspace(
                torch.min(data_dict[mod]["spatial_coords"]),
                torch.max(data_dict[mod]["spatial_coords"]),
                self.m_G,
            ).unsqueeze(1)
        )
        S_G_sqt_list = torch.zeros(
            [self.n_views, self.n_spatial_dims, self.m_X_per_view, self.m_X_per_view]
        )
        for ii in range(self.n_views):
            for jj in range(self.n_spatial_dims):
                S_sqt = 1.0 * torch.randn(size=[m_X_per_view, m_X_per_view])
                S_G_sqt_list[ii, jj, :, :] = S_sqt
        self.S_G_sqt_list = nn.Parameter(S_G_sqt_list)

        S_F_sqt_list = torch.zeros([self.n_latent_gps, self.m_G, self.m_G])
        for jj in range(self.n_latent_gps):
            S_sqt = 1.0 * torch.randn(size=[self.m_G, self.m_G])
            S_F_sqt_list[jj, :, :] = S_sqt
        self.S_F_sqt_list = nn.Parameter(S_F_sqt_list)

        self.m_G_list = nn.Parameter(self.Xtilde.clone())

        self.m_F = nn.Parameter(torch.randn([self.m_G, self.n_latent_gps]))

        self.W_dict = torch.nn.ParameterDict()
        for mod in self.modality_names:
            self.W_dict[mod] = nn.Parameter(
                torch.randn([self.n_latent_gps, self.Ps[mod]])
            )

    def forward(self, X_spatial, S=1):
        # print(self.Gtilde)
        self.noise_variance_pos = torch.exp(self.noise_variance) + 1e-4
        kernel_variances_pos = torch.exp(self.kernel_variances)
        kernel_lengthscales_pos = torch.exp(self.kernel_lengthscales)
        self.kernel_G = gp.kernels.RBF(
            input_dim=1,
            variance=kernel_variances_pos[0],
            lengthscale=kernel_lengthscales_pos[0],
        )
        # n_total = X_spatial.shape[0]

        self.mu_z_G = torch.zeros(
            [self.n_views, self.m_X_per_view, self.n_spatial_dims]
        )
        self.mu_x_G = torch.zeros(
            [X_spatial["expression"].shape[0], self.n_spatial_dims]
        )
        for vv in range(self.n_views):
            self.mu_z_G[vv] = (
                torch.mm(self.Xtilde[vv], self.mean_slopes[vv])
                + self.mean_intercepts[vv]
            )

            curr_idx = self.view_idx["expression"][vv]
            self.mu_x_G[curr_idx] = (
                torch.mm(X_spatial["expression"][curr_idx], self.mean_slopes[vv])
                + self.mean_intercepts[vv]
            )

        self.K_ZGZG = torch.zeros([self.n_views, self.m_X_per_view, self.m_X_per_view])
        self.K_ZGX = torch.zeros(
            [self.n_views, self.m_X_per_view, X_spatial["expression"].shape[0]]
        )

        self.G_samples = torch.zeros(
            [S, X_spatial["expression"].shape[0], self.n_spatial_dims]
        )
        self.G_means = torch.zeros(
            [X_spatial["expression"].shape[0], self.n_spatial_dims]
        )

        self.curr_S_G_list = torch.zeros(
            [self.n_views, self.n_spatial_dims, self.m_X_per_view, self.m_X_per_view]
        )

        for vv in range(self.n_views):

            curr_X_spatial = X_spatial["expression"][curr_idx]

            curr_idx = self.view_idx["expression"][vv]
            curr_K_ZGZG = self.kernel_G(self.Xtilde[vv], self.Xtilde[vv])
            curr_K_ZGZG += self.diagonal_offset * torch.eye(curr_K_ZGZG.shape[0])
            self.K_ZGZG[vv] = curr_K_ZGZG
            curr_K_XX = self.kernel_G(curr_X_spatial, curr_X_spatial)
            curr_K_XX += self.diagonal_offset * torch.eye(curr_K_XX.shape[0])

            curr_K_ZGX = self.kernel_G(self.Xtilde[vv], curr_X_spatial)
            # curr_K_ZGZG_inv = torch.linalg.solve(curr_K_ZGZG, torch.eye(self.m_X_per_view))
            curr_K_ZGZG_inv = torch.linalg.inv(curr_K_ZGZG)
            curr_alpha = torch.matmul(curr_K_ZGZG_inv, curr_K_ZGX)

            curr_mean_diffs = self.m_G_list[vv] - self.mu_z_G[vv]

            curr_mu_Gs = self.mu_x_G[curr_idx] + torch.matmul(
                curr_alpha.t(), curr_mean_diffs
            )

            self.G_means[curr_idx, :] = curr_mu_Gs.clone()

            for jj in range(self.n_spatial_dims):
                curr_S = torch.matmul(
                    self.S_G_sqt_list[vv, jj], self.S_G_sqt_list[vv, jj].t()
                )
                curr_S += self.diagonal_offset * torch.eye(curr_S.shape[0])
                self.curr_S_G_list[vv, jj, :, :] = curr_S
                curr_cov_diff = curr_K_ZGZG - curr_S
                curr_alphaT_cov_alpha = torch.matmul(
                    torch.matmul(curr_alpha.t(), curr_cov_diff), curr_alpha
                )
                curr_Sigma_G = curr_K_XX - curr_alphaT_cov_alpha

                curr_G_distribution = torch.distributions.MultivariateNormal(
                    loc=curr_mu_Gs[:, jj],
                    covariance_matrix=torch.diag(torch.diag(curr_Sigma_G)),
                )
                curr_G_samples = curr_G_distribution.rsample(sample_shape=[S])
                self.G_samples[:, curr_idx, jj] = curr_G_samples

        ###### SAMPLE F ######

        self.kernel_F = gp.kernels.RBF(
            input_dim=self.n_spatial_dims,
            variance=kernel_variances_pos[-1],
            lengthscale=kernel_lengthscales_pos[-1],
        )

        self.curr_S_F_list = torch.zeros([self.n_latent_gps, self.m_G, self.m_G])
        self.F_latent_samples = torch.zeros(
            [S, X_spatial["expression"].shape[0], self.n_latent_gps]
        )
        self.F_observed_samples = torch.zeros(
            [S, X_spatial["expression"].shape[0], self.Ps["expression"]]
        )

        self.curr_K_ZFZF = self.kernel_F(self.Gtilde, self.Gtilde)
        self.curr_K_ZFZF += self.diagonal_offset * torch.eye(self.curr_K_ZFZF.shape[0])

        mu_x_F = torch.zeros([X_spatial["expression"].shape[0], self.n_latent_gps])
        self.mu_z_F = torch.zeros([self.m_G, self.n_latent_gps])

        for ss in range(S):

            curr_G_sample = self.G_samples[ss]

            curr_K_GG = self.kernel_F(curr_G_sample, curr_G_sample)
            curr_K_GG += self.diagonal_offset * torch.eye(curr_K_GG.shape[0])
            curr_K_ZFX = self.kernel_F(self.Gtilde, curr_G_sample)
            # curr_K_ZFZF_inv = torch.linalg.solve(self.curr_K_ZFZF, torch.eye(self.m_G))
            curr_K_ZFZF_inv = torch.linalg.inv(self.curr_K_ZFZF)
            curr_alpha = torch.matmul(curr_K_ZFZF_inv, curr_K_ZFX)

            curr_mean_diffs = self.m_F - self.mu_z_F
            curr_mu_Fs = mu_x_F + torch.matmul(curr_alpha.t(), curr_mean_diffs)

            for jj in range(self.n_latent_gps):
                curr_S = torch.matmul(self.S_F_sqt_list[jj], self.S_F_sqt_list[jj].t())
                curr_S += self.diagonal_offset * torch.eye(curr_S.shape[0])
                self.curr_S_F_list[jj, :, :] = curr_S

                curr_cov_diff = self.curr_K_ZFZF - curr_S
                curr_alphaT_cov_alpha = torch.matmul(
                    torch.matmul(curr_alpha.t(), curr_cov_diff), curr_alpha
                )
                curr_Sigma_F = (
                    curr_K_GG
                    - curr_alphaT_cov_alpha
                    + self.diagonal_offset * torch.eye(curr_K_GG.shape[0])
                )
                curr_F_distribution = torch.distributions.MultivariateNormal(
                    loc=curr_mu_Fs[:, jj],
                    covariance_matrix=torch.diag(torch.diag(curr_Sigma_F)),
                )
                curr_F_samples = curr_F_distribution.rsample(sample_shape=[1])
                self.F_latent_samples[ss, :, jj] = curr_F_samples

            F_observed_mean = torch.matmul(
                self.F_latent_samples[0, :, :], self.W_dict["expression"]
            )
            curr_F_distribution = torch.distributions.Normal(
                F_observed_mean, self.noise_variance_pos
            )
            curr_F_observed_samples = curr_F_distribution.rsample(sample_shape=[1])
            self.F_observed_samples[ss, :, :] = curr_F_observed_samples

        return self.F_latent_samples

    def loss_fn(self, data_dict):
        # This is the negative (approximate) ELBO

        # KL terms
        KL_div = 0

        ## G
        for vv in range(self.n_views):

            for jj in range(self.n_spatial_dims):

                qu = torch.distributions.MultivariateNormal(
                    loc=self.m_G_list[vv, :, jj],
                    covariance_matrix=self.curr_S_G_list[vv, jj, :, :],
                )
                # import ipdb; ipdb.set_trace()
                pu = torch.distributions.MultivariateNormal(
                    loc=self.mu_z_G[vv, :, jj],
                    covariance_matrix=self.K_ZGZG[vv, :, :],
                )
                KL_div += torch.distributions.kl.kl_divergence(qu, pu)

        ## F
        LL = 0
        for jj in range(self.n_latent_gps):
            qu = torch.distributions.MultivariateNormal(
                loc=self.m_F[:, jj],
                covariance_matrix=self.curr_S_F_list[jj, :, :],
            )

            pu = torch.distributions.MultivariateNormal(
                loc=self.mu_z_F[:, jj],
                covariance_matrix=self.curr_K_ZFZF,
            )
            KL_div += torch.distributions.kl.kl_divergence(qu, pu)

        for jj in range(self.Ps["expression"]):
            # Log likelihood
            Y_distribution = torch.distributions.Normal(
                loc=self.F_observed_samples[:, :, jj], scale=self.noise_variance_pos
            )
            LL += Y_distribution.log_prob(data_dict["expression"]["outputs"][:, jj])

        KL_loss = self.n_spatial_dims * KL_div
        LL_loss = -torch.sum(torch.mean(LL, dim=0))

        mean_penalty = self.compute_mean_penalty()
        # print(LL_loss, KL_loss)
        return LL_loss + KL_loss  # + mean_penalty
        # return LL_loss


class VGPRDataset(Dataset):
    def __init__(self, X, Y):
        self.X = X
        self.Y = Y
        assert X.shape[0] == Y.shape[0]

    def __len__(self):
        return self.Y.shape[0]

    def __getitem__(self, idx):
        return {"X": self.X[idx, :], "Y": self.Y[idx]}


if __name__ == "__main__":
    pass
