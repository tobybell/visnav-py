# visnav-py
Test Framework for Visual Navigation Algorithms

# Installation
Needs:
Python >3.3, pyqt5, scipy, quaternion, astropy, opencv3
VisIt: https://wci.llnl.gov/simulation/computer-codes/visit

At least on Windows, to get necessary Python packages it's easiest to use Anaconda
https://www.continuum.io/downloads

After installing Anaconda, run from command prompt:
conda install -c menpo opencv3=3.1.0
conda install -c moble quaternion

Download data files from
https://drive.google.com/drive/folders/0ByfhOdRO_959X05jTWczWGxLUkk?usp=sharing
into data/ folder

To run standalone GUI mode in Windows:
"C:\Program Files\Anaconda3\python" src\visnav.py

To run a Monte Carlo batch, open batch1.py in an editor to see what you are going to run, then:
"C:\Program Files\Anaconda3\python" src\batch1.py

You also might want to look at src/settings.py.

# Documentation
This work started as a project done at Aalto University, School of Electrical Engineering.
The documentation done towards those credits can be found at
https://docs.google.com/document/d/1lXqXdR02dAcGPsClwZOXj39RbBfrcscxIKrUyMY_WGU/edit#heading=h.dw2dac9r7xzm
