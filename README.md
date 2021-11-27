![celeri logo](https://user-images.githubusercontent.com/4225359/132613223-257e6e17-83bd-49a4-8bbc-326cc117f6ec.png)

<img src="https://some-img-host.com/1234567/image.png" width=300 align=right>

## celeri
- Next generation earthquake cycle and surface deformation modeling
- A python port, reworking, and extension of the Matlab-based [blocks](https://github.com/jploveless/Blocks).

To set up a development conda environment, run the following commands in the `celeri` folder.
```
conda config --prepend channels conda-forge
conda env create
pip install --no-use-pep517 -e .
```

Then start your favorite notebook viewer (`jupyter lab` of `vscode`) and open and run `celeri.ipynb`.
