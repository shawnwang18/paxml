from ctypes import cdll
import tensorflow as tf 
libcudart = cdll.LoadLibrary('libcudart.so')
def cudaProfilerStart():
    libcudart.cudaProfilerStart()
def cudaProfilerStop():
    libcudart.cudaProfilerStop()
def cudaDeviceSynchronize():
    libcudart.cudaDeviceSynchronize()