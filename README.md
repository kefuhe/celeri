<p align="center">
  <img src="https://user-images.githubusercontent.com/4225359/132613223-257e6e17-83bd-49a4-8bbc-326cc117f6ec.png" width=400 />
</p>

## celeri - Next generation earthquake cycle and surface deformation modeling
A python port, reworking, and extension of the Matlab-based [blocks](https://github.com/jploveless/Blocks) featuring:
- Much smaller memory footprint
- Much faster elastic calculations
- Much faster block closure
- Eigenfunction expansion for partial coupling

To set up a development conda environment, run the following commands in the `celeri` folder.
```
conda config --prepend channels conda-forge
conda env create
conda activate celeri
pip install --no-use-pep517 -e .
```

Then start your favorite notebook viewer (`jupyter lab` or `vscode`) and open and run `celeri.ipynb`.

### Relationships of input files
```mermaid
  flowchart TD;
      command.json-->segment.csv;
      command.json-->station.csv;
      command.json-->block.csv;
      command.json-->los.csv;
      command.json<-->elastic.hdf5;
      command.json-->mesh_parameters.json;
      mesh_parameters.json-->mesh_1.msh;
      mesh_parameters.json-->mesh_2.msh;
      mesh_parameters.json-->mesh_?.msh;
      subgraph input_files
```
