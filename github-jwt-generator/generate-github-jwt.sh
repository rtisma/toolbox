#!/bin/bash

#Github App Id
appId=$1

# Github APP Pem file
pemFile=$2

rootDir=$(dirname $(realpath ${BASH_SOURCE[0]}))
imageName=generate-gh-jwt:local

if [ -z ${appId} ]; then
	echo "The first param (appId) was not defined"
	exit 1
fi

if [ -z ${pemFile} ]; then
	echo "The second param (pemFile path) was not defined"
	exit 1
elif [ ! -f ${pemFile} ]; then
	echo "The pemFile \"${pemFile}\" does not exist"
	exit 1
fi

buildout=$(docker build ${rootDir} -f ${rootDir}/Dockerfile -t ${imageName} 2>&1)
buildError=$?
if ((buildError)); then
	echo >&2 "BUILD_ERROR($buildError): \"$buildout\""
	exit 1
fi
docker run --rm -it -e "GH_APP_ID=${appId}" -v "${pemFile}:/etc/github-private.pem" ${imageName}

