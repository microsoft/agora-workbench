FROM mcr.microsoft.com/azureml/curated/acpt-pytorch-2.2-cuda12.1:48
ARG CONDA_PREFIX=/opt/mamba-forge
ENV PIP_NO_CACHE_DIR=1
ENV CUDA_HOME=/usr/local/cuda
ENV HIGHS_BUILD_GPU=ON
ENV ENV GRADIO_ANALYTICS_ENABLED=False


## System packages (combine to reduce layers)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git build-essential ca-certificates wget \
    nvtop tmux vim \
    && \
    rm -rf /var/lib/apt/lists/*

## Azure CLI (official script with verification flags)
RUN curl -fsSL https://aka.ms/InstallAzureCLIDeb | bash

## Install Miniforge (verified TLS)
RUN wget -q "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname)-$(uname -m).sh" -O /tmp/miniforge.sh && \
    bash /tmp/miniforge.sh -b -p ${CONDA_PREFIX} && \
    rm /tmp/miniforge.sh

## Install mamba in base environment (Miniforge doesn't include mamba by default)
RUN ${CONDA_PREFIX}/bin/conda install -n base -y mamba && \
    ${CONDA_PREFIX}/bin/conda clean -afy

RUN apt-get update && apt-get install -y libpq-dev python3-dev gcc pkg-config


RUN ${CONDA_PREFIX}/bin/mamba create -n agoragrids python=3.10 ipopt -y && \
    ${CONDA_PREFIX}/bin/mamba clean --all --yes

## Make conda auto-activate for interactive shells
RUN echo ". ${CONDA_PREFIX}/etc/profile.d/conda.sh" >> /etc/bash.bashrc && \
    echo "conda activate agoragrids" >> /etc/bash.bashrc

# Download the latest installer
ADD https://astral.sh/uv/install.sh /uv-installer.sh

# Run the installer to install uv globally then remove it
RUN sh /uv-installer.sh && mv /root/.local/bin/uv /usr/local/bin/uv && rm /uv-installer.sh

SHELL ["/bin/bash","-c"]

# Add a non-root user for security; make conda and uv accessible to all users
RUN adduser --disabled-password --gecos '' devuser && \
    chmod -R a+rX ${CONDA_PREFIX}
USER devuser