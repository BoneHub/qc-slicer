cmake_minimum_required(VERSION 3.28.0...3.28.0 FATAL_ERROR)

project(BoneHubDatasetQualityCheck)

#-----------------------------------------------------------------------------
# Extension meta-information
set(EXTENSION_HOMEPAGE "https://github.com/BoneHub/bonehub_dataset_quality_check_3dslicer_extension")
set(EXTENSION_CATEGORY "Segmentation")
set(EXTENSION_CONTRIBUTORS "Hamid Alavi (University of Twente)")
set(EXTENSION_DESCRIPTION "Reviewer client for the BoneHub dataset quality-check server. Leases one subject at a time, loads its image and segmentation, and sends the reviewed segmentation back.")
set(EXTENSION_ICONURL "https://raw.githubusercontent.com/BoneHub/bonehub_dataset_quality_check_3dslicer_extension/main/BoneHubDatasetQualityCheck.png")
set(EXTENSION_SCREENSHOTURLS "")
set(EXTENSION_DEPENDS "NA") # Specified as a list or "NA" if no dependencies

#-----------------------------------------------------------------------------
# Extension dependencies
find_package(Slicer REQUIRED)
include(${Slicer_USE_FILE})

#-----------------------------------------------------------------------------
# Extension modules
add_subdirectory(BoneHubQualityCheck)
## NEXT_MODULE

#-----------------------------------------------------------------------------
include(${Slicer_EXTENSION_GENERATE_CONFIG})
include(${Slicer_EXTENSION_CPACK})
